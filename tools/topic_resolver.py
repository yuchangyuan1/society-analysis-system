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
    content_fingerprint)` where the fingerprint is md5 over
    `topic_id || label || centroid_text` for every row. Keyed on content,
    not `updated_at`, because the `posts_v2` -> `topics_v2.post_count`
    trigger bumps `updated_at` on every ingest even when label/centroid
    haven't changed - using updated_at would force a full re-embed after
    every pipeline run.
"""
from __future__ import annotations

import math
import re
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
        return self.resolve_candidates(phrase, top_k=top_k)

    def resolve_candidates(
        self,
        phrase: str,
        top_k: Optional[int] = None,
        *,
        include_semantic_alternatives: bool = False,
        min_similarity: Optional[float] = None,
    ) -> list[TopicMatch]:
        """Return topic candidates, optionally adding semantic alternatives.

        Exact label matches are still returned first. KG callers can ask for
        semantic alternatives so a graph-empty exact topic can fall back to a
        semantically close topic with actual Kuzu edges.
        """
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

        k = top_k if top_k is not None else self._top_k
        exact = _exact_label_matches(phrase, self._cache.rows)
        if exact and not include_semantic_alternatives:
            return exact[:k]

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
        floor = self._min_similarity if min_similarity is None else min_similarity
        cutoff = max(floor, top_sim / self._gap_ratio)
        kept = [m for m in scored if m.similarity >= cutoff]
        # Always return at least the top match if it clears the floor.
        if not kept and top_sim >= floor:
            kept = [scored[0]]
        if not exact:
            return kept[:k]

        out: list[TopicMatch] = []
        seen: set[str] = set()
        for match in exact + kept:
            if match.topic_id in seen:
                continue
            seen.add(match.topic_id)
            out.append(match)
            if len(out) >= k:
                break
        return out

    # ── Cache management ──────────────────────────────────────────────────────

    def _refresh_cache(self) -> None:
        if self._pg is None:
            self._pg = PostgresService()
            self._pg.connect()
        with self._pg.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n, "
                "       COALESCE(md5(string_agg("
                "           topic_id || '|' || COALESCE(label, '') "
                "           || '|' || COALESCE(centroid_text, ''), "
                "           chr(10) ORDER BY topic_id)), '') AS fp "
                "FROM topics_v2"
            )
            stats = cur.fetchone() or {}
            cache_key = (int(stats.get("n", 0)), str(stats.get("fp", "")))

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


def _exact_label_matches(phrase: str, rows: list[dict]) -> list[TopicMatch]:
    """Prefer exact/contained topic labels before semantic nearest-neighbor.

    Users often paste or paraphrase a generated topic label inside a longer
    instruction: "trace propagation for US Military Arms Sales Controversy".
    In that case an embedding-only resolver can choose a nearby sibling topic
    with no KG signal. A normalized label containment match is more precise.
    """
    phrase_norm = _normalize_topic_text(phrase)
    if not phrase_norm:
        return []
    matches: list[tuple[int, int, TopicMatch]] = []
    for row in rows:
        label = row.get("label") or ""
        label_norm = _normalize_topic_text(label)
        if not label_norm or len(label_norm) < 8:
            continue
        if (
            phrase_norm == label_norm
            or label_norm in phrase_norm
            or phrase_norm in label_norm
        ):
            matches.append((
                len(label_norm),
                int(row.get("post_count") or 0),
                TopicMatch(
                    topic_id=row["topic_id"],
                    label=label or row["topic_id"],
                    similarity=1.0,
                ),
            ))
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [m for _, _, m in matches]


def _normalize_topic_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()
