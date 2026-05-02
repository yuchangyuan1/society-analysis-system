"""
Entity Extractor — redesign-2026-05 Phase 1.4.

Extracts PERSON / ORG / LOC / EVENT entities directly from post text
(including image OCR/caption folded back via `Post.merged_text()`) and
writes `EntitySpan` objects into `post.entities`.

Replaces the v1 path KnowledgeAgent.extract_entities, which is bound to
claims.

Usage:
    extractor = EntityExtractor()
    extractor.extract_for_posts(posts)
    for p in posts:
        for e in p.entities:
            ...

Notes:
- Reuses the prompt style of v1 KnowledgeAgent's `_ENTITY_SYSTEM`, but
  aligns the type set with v2: PERSON / ORG / LOC / EVENT / OTHER (v1 uses
  PLACE; here it becomes LOC).
- Batch size capped at 30 posts per LLM call (cost + token-window guard).
- Failures do not raise; they log and leave post.entities empty.
"""
from __future__ import annotations

import json
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.entity import EntitySpan
from models.post import Post

log = structlog.get_logger(__name__)


_ENTITY_SYSTEM = """You are a named entity extractor for social-media posts.
Given a numbered list of posts, return STRICT JSON: an array of objects, each with:
  {"post_idx": int, "name": str, "type": one of [PERSON, ORG, LOC, EVENT]}

Rules:
- post_idx is the 1-based index from the input list
- Return at most 5 entities per post
- Normalize abbreviations (e.g. "WHO" -> "World Health Organization")
- Skip generic terms (e.g. "people", "country")
- Use LOC for places (cities, countries, regions); not PLACE
- Output JSON array only. No prose."""


class EntityExtractor:
    """LLM-backed entity extractor with post-level batching."""

    def __init__(
        self,
        client: Optional[openai.OpenAI] = None,
        model: str = OPENAI_MODEL,
        batch_size: int = 30,
    ) -> None:
        self._client = client or openai.OpenAI(api_key=OPENAI_API_KEY)
        self._model = model
        self._batch_size = batch_size

    # ── Public ─────────────────────────────────────────────────────────────────

    def extract_for_posts(self, posts: list[Post]) -> int:
        """Extract entities for a batch of posts (in-place). Returns total count."""
        if not posts:
            return 0

        total = 0
        for batch_start in range(0, len(posts), self._batch_size):
            batch = posts[batch_start:batch_start + self._batch_size]
            extracted = self._extract_batch(batch)
            total += extracted

        log.info("entity_extractor.done",
                 posts=len(posts), entities=total)
        return total

    # ── Internal ───────────────────────────────────────────────────────────────

    def _extract_batch(self, batch: list[Post]) -> int:
        """One LLM call for a batch; fills entities in place."""
        # Use merged_text so OCR / captions also feed the extraction
        numbered = "\n".join(
            f"{i + 1}. {p.merged_text()[:400]}"
            for i, p in enumerate(batch)
        )

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                max_tokens=1024,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": _ENTITY_SYSTEM},
                    {"role": "user", "content": numbered[:8000]},
                ],
            )
        except Exception as exc:
            log.error("entity_extractor.api_error",
                      error=str(exc)[:120], batch_size=len(batch))
            return 0

        raw = (resp.choices[0].message.content or "[]").strip()
        items = self._parse_json_array(raw)

        valid_types = {"PERSON", "ORG", "LOC", "EVENT"}
        # Group by post_idx
        per_post: dict[int, list[EntitySpan]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("post_idx")
            name = (item.get("name") or "").strip()
            etype = (item.get("type") or "OTHER").upper()
            if not isinstance(idx, int) or not name:
                continue
            if etype not in valid_types:
                etype = "OTHER"
            span = EntitySpan(name=name, entity_type=etype, confidence=0.7)
            per_post.setdefault(idx, []).append(span)

        # Write back
        count = 0
        for idx_one_based, spans in per_post.items():
            real_idx = idx_one_based - 1
            if not (0 <= real_idx < len(batch)):
                continue
            # Dedupe (by name + type)
            seen: set[tuple] = set()
            unique: list[EntitySpan] = []
            for s in spans:
                key = (s.name.lower(), s.entity_type)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(s)
            batch[real_idx].entities = unique
            count += len(unique)

        return count

    @staticmethod
    def _parse_json_array(raw: str) -> list:
        """Tolerant JSON-array parser that strips markdown code fences."""
        if raw.startswith("```"):
            parts = raw.split("```")
            if len(parts) > 1:
                raw = parts[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
        start, end = raw.find("["), raw.rfind("]") + 1
        if start != -1 and end > start:
            raw = raw[start:end]
        try:
            return json.loads(raw) if raw else []
        except json.JSONDecodeError:
            log.warning("entity_extractor.json_parse_error", raw=raw[:120])
            return []
