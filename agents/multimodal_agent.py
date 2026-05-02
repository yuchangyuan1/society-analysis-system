"""
Multimodal Agent — redesign-2026-05 Phase 1.3.

Converts post images into text descriptions and folds them back into the
post text, so downstream text-only modules (topic clustering, NL2SQL,
entity extraction) can see image content.

Design notes:
- Sampling (Q6 option B): only run on high-engagement posts
  (`like_count >= MIN_LIKES OR reply_count >= MIN_REPLIES`); skip the rest.
- Daily budget (`MULTIMODAL_DAILY_BUDGET_USD`): once accumulated estimated
  spend exceeds the daily cap, remaining calls are skipped silently.
- Reuses `services.claude_vision_service.ClaudeVisionService`; does not
  reimplement vision invocation.
- Does NOT do claim extraction: the v1 `candidate_claims` field is ignored
  (v2 has no claim arm).

Phase 2 will wire entity_extractor and this agent together inside the
ingest stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import structlog

from config import (
    MULTIMODAL_COST_PER_CALL_USD,
    MULTIMODAL_DAILY_BUDGET_USD,
    MULTIMODAL_MIN_LIKES,
    MULTIMODAL_MIN_REPLIES,
)
from models.post import Post
from services.claude_vision_service import ClaudeVisionService

log = structlog.get_logger(__name__)


@dataclass
class BudgetTracker:
    """Daily spend bookkeeping for image-understanding calls."""
    daily_budget_usd: float
    cost_per_call_usd: float
    consumed_usd: float = 0.0
    calls: int = 0
    skipped_for_budget: int = 0

    def can_spend(self) -> bool:
        return self.consumed_usd + self.cost_per_call_usd <= self.daily_budget_usd

    def record_call(self) -> None:
        self.consumed_usd += self.cost_per_call_usd
        self.calls += 1

    def record_skip_budget(self) -> None:
        self.skipped_for_budget += 1


@dataclass
class EnrichmentSummary:
    """Result summary for a single enrich_posts invocation."""
    posts_total: int = 0
    posts_with_images: int = 0
    posts_eligible_by_engagement: int = 0
    images_processed: int = 0
    images_skipped_engagement: int = 0
    images_skipped_budget: int = 0
    errors: int = 0
    consumed_usd: float = 0.0


class MultimodalAgent:
    """
    Takes a batch of Posts (with ImageAssets) and fills ocr_text /
    image_caption / image_type in place. `Post.merged_text()` then includes
    the image content automatically.
    """

    def __init__(
        self,
        vision: Optional[ClaudeVisionService] = None,
        *,
        daily_budget_usd: float = MULTIMODAL_DAILY_BUDGET_USD,
        cost_per_call_usd: float = MULTIMODAL_COST_PER_CALL_USD,
        min_likes: int = MULTIMODAL_MIN_LIKES,
        min_replies: int = MULTIMODAL_MIN_REPLIES,
    ) -> None:
        self._vision = vision or ClaudeVisionService()
        self._budget = BudgetTracker(
            daily_budget_usd=daily_budget_usd,
            cost_per_call_usd=cost_per_call_usd,
        )
        self._min_likes = min_likes
        self._min_replies = min_replies

    # ── Public ─────────────────────────────────────────────────────────────────

    def enrich_posts(self, posts: list[Post]) -> EnrichmentSummary:
        """Run image understanding over a batch of posts (in-place)."""
        summary = EnrichmentSummary(posts_total=len(posts))

        for post in posts:
            if not post.has_image:
                continue
            summary.posts_with_images += 1

            if not self._is_high_engagement(post):
                summary.images_skipped_engagement += len(post.images)
                continue
            summary.posts_eligible_by_engagement += 1

            for img in post.images:
                # Idempotent: skip already-analysed images
                if img.image_caption or img.ocr_text:
                    continue

                if not self._budget.can_spend():
                    self._budget.record_skip_budget()
                    summary.images_skipped_budget += 1
                    log.warning(
                        "multimodal.budget_exhausted",
                        consumed_usd=round(self._budget.consumed_usd, 4),
                        daily_budget_usd=self._budget.daily_budget_usd,
                    )
                    continue

                try:
                    result = self._vision.analyze_image(
                        image_path=img.local_path,
                        image_url=img.url,
                        post_text=post.text or "",
                    )
                    img.ocr_text = result.get("ocr_text", "") or None
                    img.image_caption = result.get("image_caption", "") or None
                    img.image_type = result.get("image_type", "other")
                    self._budget.record_call()
                    summary.images_processed += 1
                except Exception as exc:
                    log.error("multimodal.vision_error",
                              post_id=post.id, error=str(exc)[:120])
                    summary.errors += 1

        summary.consumed_usd = round(self._budget.consumed_usd, 4)
        log.info("multimodal.enrich_done",
                 posts=summary.posts_total,
                 with_images=summary.posts_with_images,
                 processed=summary.images_processed,
                 skipped_engagement=summary.images_skipped_engagement,
                 skipped_budget=summary.images_skipped_budget,
                 errors=summary.errors,
                 consumed_usd=summary.consumed_usd)
        return summary

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _is_high_engagement(self, post: Post) -> bool:
        return (post.like_count >= self._min_likes
                or post.reply_count >= self._min_replies)
