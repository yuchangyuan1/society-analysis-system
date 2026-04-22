"""
Ingestion Workspace — post-ingest + image-post-ingest skills.

Responsibilities:
  - Fetch posts from Telegram (primary) or X API (fallback) or JSONL file
  - For each image post: OCR + caption via Claude Vision
  - Persist posts to Kuzu + Postgres
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Optional, Union

import structlog

from models.post import Post, ImageAsset
from services.claude_vision_service import ClaudeVisionService
from services.kuzu_service import KuzuService
from services.postgres_service import PostgresService
from services.telegram_service import TelegramService
from services.x_api_service import XApiService
from services.reddit_service import RedditService

log = structlog.get_logger(__name__)


class IngestionAgent:
    def __init__(
        self,
        pg: PostgresService,
        kuzu: KuzuService,
        vision: ClaudeVisionService,
        telegram: Optional[TelegramService] = None,
        x_api: Optional[XApiService] = None,
        reddit: Optional[RedditService] = None,
    ) -> None:
        self._pg = pg
        self._kuzu = kuzu
        self._vision = vision
        # Prefer Telegram; fall back to X API if Telegram not configured
        self._telegram = telegram
        self._x_api = x_api
        self._reddit = reddit

    @property
    def _social_api(self) -> Union[TelegramService, XApiService, None]:
        """Return the active social media API client."""
        if self._telegram and self._telegram._available:
            return self._telegram
        if self._x_api:
            return self._x_api
        return None

    # ── Skill: post-ingest ────────────────────────────────────────────────────

    def ingest_posts_from_query(
        self,
        query: str,
        max_results: int = 50,
        channels: Optional[list[str]] = None,
    ) -> list[Post]:
        """
        Fetch posts matching query from the active social API.
        - Telegram: searches across `channels` (or default misinfo channels)
        - X API fallback: uses sanitized keyword search
        """
        api = self._social_api
        if api is None:
            log.warning("ingestion.no_api_configured")
            return []

        search_query = self._sanitize_search_query(query)

        if isinstance(api, TelegramService):
            posts = api.search_messages(
                search_query, channels=channels, max_results=max_results
            )
        else:
            posts = api.search_recent(search_query, max_results=max_results)

        log.info("ingestion.fetched_posts",
                 count=len(posts), query=search_query,
                 source="telegram" if isinstance(api, TelegramService) else "x_api")
        for post in posts:
            self._store_post(post)
        return posts

    def ingest_channel_today(
        self,
        channel: str,
        date=None,
        days_back: int = 1,
    ) -> list[Post]:
        """
        Fetch posts from `channel` for the last `days_back` days and store
        them in Chroma, Kuzu, and Postgres.

        days_back=1  → today only (auto-expands to 7 if 0 posts found)
        days_back=7  → last week
        """
        if not (self._telegram and self._telegram._available):
            log.error("ingestion.telegram_not_configured")
            return []
        posts = self._telegram.get_channel_today(channel, date=date, days_back=days_back)
        log.info("ingestion.channel_today", channel=channel, count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    def ingest_posts_from_multi_keywords(
        self,
        keywords: list[str],
        max_per_query: int = 20,
        channels: Optional[list[str]] = None,
    ) -> list[Post]:
        """
        Fetch posts for multiple keywords from the active social API.
        """
        api = self._social_api
        if api is None:
            log.warning("ingestion.no_api_configured")
            return []

        if isinstance(api, TelegramService):
            posts = api.search_multi_keywords(
                keywords, channels=channels, max_per_query=max_per_query
            )
        else:
            posts = api.search_multi_keywords(keywords, max_per_query=max_per_query)

        log.info("ingestion.multi_keyword_fetched",
                 count=len(posts),
                 source="telegram" if isinstance(api, TelegramService) else "x_api")
        for post in posts:
            self._store_post(post)
        return posts

    # ── Reddit ingestion ──────────────────────────────────────────────────────

    def ingest_subreddit(
        self,
        subreddit: str,
        sort: str = "hot",
        days_back: int = 7,
        limit: int = 100,
    ) -> list[Post]:
        """
        Fetch posts + top-level comments from a subreddit and store them.

        Parameters
        ----------
        sort      : "hot" | "new" | "top" | "rising"
        days_back : only include posts from the last N days
        limit     : max number of submissions to request from Reddit
        """
        if not (self._reddit and self._reddit._available):
            log.error("ingestion.reddit_not_configured")
            return []
        posts = self._reddit.get_subreddit_posts(
            subreddit, sort=sort, limit=limit, days_back=days_back
        )
        log.info("ingestion.reddit_subreddit",
                 subreddit=subreddit, count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    def ingest_reddit_search(
        self,
        query: str,
        subreddits: Optional[list[str]] = None,
        days_back: int = 7,
        limit: int = 100,
    ) -> list[Post]:
        """
        Search Reddit for `query` across subreddits and store results.
        If subreddits is None, searches all of Reddit.
        """
        if not (self._reddit and self._reddit._available):
            log.error("ingestion.reddit_not_configured")
            return []
        posts = self._reddit.search_posts(
            query, subreddits=subreddits, days_back=days_back, limit=limit
        )
        log.info("ingestion.reddit_search",
                 query=query[:60], count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    def ingest_multi_subreddit(
        self,
        subreddits: Optional[list[str]] = None,
        sort: str = "hot",
        days_back: int = 7,
        limit_per_sub: int = 50,
    ) -> list[Post]:
        """
        Aggregate posts from multiple subreddits (defaults to config list).
        """
        if not (self._reddit and self._reddit._available):
            log.error("ingestion.reddit_not_configured")
            return []
        posts = self._reddit.get_multi_subreddit(
            subreddits=subreddits, sort=sort,
            limit_per_sub=limit_per_sub, days_back=days_back,
        )
        log.info("ingestion.reddit_multi",
                 subreddits=subreddits or "defaults", count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    def ingest_posts_from_jsonl(self, filepath: str) -> list[Post]:
        """Load pre-collected posts from a JSONL file and store them."""
        posts = self._x_api.load_from_jsonl(filepath)
        log.info("ingestion.loaded_jsonl", count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    # ── Skill: image-post-ingest ──────────────────────────────────────────────

    def process_image_post(
        self,
        post: Post,
        image_url: Optional[str] = None,
        image_path: Optional[str] = None,
    ) -> Post:
        """
        Full multimodal ingestion pipeline for a post with images.
        Updates post.images in-place; stores updated data back to stores.
        """
        updated_images: list[ImageAsset] = []
        for img in post.images:
            url = image_url or img.url
            path = image_path or img.local_path
            vision_result = self._vision.analyze_image(
                image_url=url, image_path=path, post_text=post.text
            )
            img.ocr_text = vision_result.get("ocr_text", "")
            img.image_caption = vision_result.get("image_caption", "")
            img.image_type = vision_result.get("image_type", "other")
            img.candidate_claims = vision_result.get("candidate_claims", [])
            if vision_result.get("image_text_unavailable"):
                log.warning(
                    "ingestion.image_text_unavailable",
                    post_id=post.id,
                    image_id=img.id,
                    reason=vision_result.get("error", ""),
                )
            # Persist
            self._pg.upsert_image(
                image_id=img.id,
                post_id=post.id,
                url=img.url,
                local_path=img.local_path,
                ocr_text=img.ocr_text,
                image_caption=img.image_caption,
                image_type=img.image_type,
                embedding_id=img.embedding_id,
            )
            updated_images.append(img)
        post.images = updated_images
        return post

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_search_query(query: str) -> str:
        """
        Convert a free-form user sentence into a short X API v2 search query.
        Strips instruction words and trims to ≤60 chars so the API doesn't
        reject it with 'Ambiguous use of and/or as a keyword'.
        """
        import re
        # Drop common instruction prefixes
        strip_phrases = [
            r"^analyze\s+(propagation\s+of\s+)?",
            r"^search\s+for\s+",
            r"^find\s+posts?\s+(about\s+)?",
            r"and\s+generate\s+a\s+counter[\s-]message",
            r"generate\s+a\s+counter[\s-]message",
            r"and\s+counter[\s-]message.*$",
            r"counter[\s-]message.*$",
        ]
        q = query.strip().lower()
        for pat in strip_phrases:
            q = re.sub(pat, " ", q, flags=re.IGNORECASE).strip()
        # Collapse whitespace
        q = re.sub(r"\s+", " ", q).strip()
        # Hard cap at 60 chars (X API recommendation for search_recent)
        if len(q) > 60:
            q = q[:60].rsplit(" ", 1)[0]
        return q or query[:60]

    def _store_post(self, post: Post) -> None:
        # Postgres — non-fatal: system degrades gracefully without it
        try:
            self._pg.upsert_account(post.account_id, post.account_id)
            self._pg.upsert_post(
                post_id=post.id,
                account_id=post.account_id,
                text=post.text,
                lang=post.lang,
                retweet_count=post.retweet_count,
                like_count=post.like_count,
                reply_count=post.reply_count,
                has_image=post.has_image,
                posted_at=post.posted_at,
            )
        except Exception as exc:
            log.warning("ingestion.postgres_skip", post_id=post.id, error=str(exc)[:80])
        # Kuzu
        self._kuzu.upsert_account(post.account_id, post.account_id)
        self._kuzu.upsert_post(post.id, post.text)
        self._kuzu.add_posted(post.account_id, post.id)
