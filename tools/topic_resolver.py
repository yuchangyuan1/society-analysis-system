"""
Topic Resolver - redesign-2026-05 follow-up.

Maps a natural-language topic phrase ("the vaccine misinformation topic",
"climate stuff", "what people are saying about Iran") to the specific
`topics_v2.topic_id`s that are semantically closest.

Why this exists:
    Topic labels in `topics_v2.label` are LLM-generated phrases like
    "Media Trust and Vaccine Information" or "Mixed Economic and Climate
    News". Users almost never type those phrases verbatim. A literal
    LOWER(label) LIKE '%vaccine%' works only when the user's word happens
    to be inside the label - not robust.

Approach:
    1. Pull every topic (id, label, centroid_text) from topics_v2.
    2. Embed `label || ' | ' || centroid_text` for each.
    3. Embed the user phrase.
    4. Return top-K topics with cosine similarity above a threshold.

Caching:
    The embedding work is cached in-memory keyed by `(row_count,
    max_updated_at)` so we don't re-embed every call. Invalidated
    automatically when topics_v2 changes.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from services.embeddings_service import EmbeddingsService
from services.postgres_service import PostgresService

log = structlog.get_logger(__name__)


@dataclass
class TopicMatch:
    topic_id: str
    label: str
    similarity: float


@dataclass
class _TopicCache:
    cache_key: tuple = ()
    rows: list[dict] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)


class TopicResolver:
    def __init__(
        self,
        pg: Optional[PostgresService] = None,
        embeddings: Optional[EmbeddingsService] = None,
        *,
        top_k: int = 3,
        min_similarity: float = 0.22,
        gap_ratio: float = 1.5,
    ) -> None:
        self._pg = pg
        self._embeddings = embeddings or EmbeddingsService()
        self._top_k = top_k
        self._min_similarity = min_similarity
        # gap_ratio: keep a topic only if its similarity >= top_match / gap_ratio.
        # Anchors the cutoff to the BEST match; avoids "everything looks roughly
        # the same" false positives.
        self._gap_ratio = gap_ratio
        self._cache = _TopicCache()
        self._lock = threading.Lock()

    # ── Public ────────────────────────────────────────────────────────────────

    def resolve(self, phrase: str, top_k: Optional[int] = None) -> list[TopicMatch]:
        """Return up to `top_k` topics ranked by cosine similarity to `phrase`."""
        phrase = (phrase or "").strip()
        if not phrase:
            return []
        try:
            self._refresh_cache()
        except Exception as exc:
            log.warning("topic_resolver.refresh_failed", error=str(exc)[:160])
            return []
        if not self._cache.rows:
            return []

        try:
            query_vec = self._embeddings.embed(phrase)
        except Exception as exc:
            log.warning("topic_resolver.embed_failed", error=str(exc)[:160])
            return []

        scored: list[TopicMatch] = []
        for row, vec in zip(self._cache.rows, self._cache.embeddings):
            sim = _cosine(query_vec, vec)
            scored.append(TopicMatch(
                topic_id=row["topic_id"],
                label=row.get("label") or row["topic_id"],
                similarity=sim,
            ))
        scored.sort(key=lambda m: m.similarity, reverse=True)
        if not scored:
            return []
        # Anchor cutoff to the top match: keep entries that are at least
        # 1/gap_ratio of the best score AND above the absolute floor.
        top_sim = scored[0].similarity
        cutoff = max(self._min_similarity, top_sim / self._gap_ratio)
        kept = [m for m in scored if m.similarity >= cutoff]
        # Always return at least the top match if it clears the floor.
        if not kept and top_sim >= self._min_similarity:
            kept = [scored[0]]
        k = top_k if top_k is not None else self._top_k
        return kept[:k]

    # ── Cache management ──────────────────────────────────────────────────────

    def _refresh_cache(self) -> None:
        if self._pg is None:
            self._pg = PostgresService()
            self._pg.connect()
        with self._pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n, COALESCE(MAX(updated_at), NOW()) AS m "
                "FROM topics_v2"
            )
            stats = cur.fetchone() or {}
            cache_key = (int(stats.get("n", 0)), str(stats.get("m", "")))

        with self._lock:
            if cache_key == self._cache.cache_key and self._cache.rows:
                return  # still fresh
            with self._pg.cursor() as cur:
                cur.execute(
                    "SELECT topic_id, label, centroid_text, post_count, "
                    "       dominant_emotion "
                    "FROM topics_v2"
                )
                rows = list(cur.fetchall())
            if not rows:
                self._cache = _TopicCache(cache_key=cache_key)
                return
            texts = [
                f"{r.get('label') or ''} | {r.get('centroid_text') or ''}"
                for r in rows
            ]
            try:
                vectors = self._embeddings.embed_batch(texts)
            except Exception as exc:
                log.warning("topic_resolver.batch_embed_failed",
                            error=str(exc)[:160])
                vectors = [self._embeddings.embed(t) for t in texts]
            self._cache = _TopicCache(
                cache_key=cache_key, rows=rows, embeddings=vectors,
            )
            log.info("topic_resolver.cache_refreshed",
                     topics=len(rows), key=str(cache_key))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)
