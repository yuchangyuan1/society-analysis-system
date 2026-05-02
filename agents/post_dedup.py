"""
Post dedup - redesign-2026-05 Phase 2.8.

Two-tier near-duplicate detection (PROJECT_REDESIGN_V2.md 5b end + 7c-F):

    Tier 1 (primary):
        64-bit simhash. Hamming distance <= 3 -> duplicate.
    Tier 2 (long-text fallback):
        For posts with >500 tokens, simhash degrades; rerun with
        Postgres pg_trgm `similarity()` >= 0.85 -> duplicate.

Notes:
- Reasons for not using embedding: 100x slower than simhash for ingestion;
  topic clusterer already embeds posts; PROJECT_REDESIGN_V2.md decided
  posts are NOT vectorised (no Chroma posts collection).
- Operates on Posts in-memory first (current batch). PG long-text fallback
  is left as an opt-in hook so unit tests do not need a database.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

import structlog

from models.post import Post

log = structlog.get_logger(__name__)


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_LONG_TEXT_TOKEN_THRESHOLD = 500


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def compute_simhash(text: str, *, bits: int = 64) -> int:
    """Charikar simhash over whitespace tokens (no stemming).
    Returns an unsigned int in [0, 2**bits).
    """
    tokens = tokenize(text)
    if not tokens:
        return 0
    weights = [0] * bits
    for tok in tokens:
        h = int.from_bytes(
            hashlib.blake2b(tok.encode("utf-8"), digest_size=bits // 8).digest(),
            "big",
        )
        for i in range(bits):
            weights[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i, w in enumerate(weights):
        if w > 0:
            out |= (1 << i)
    # Postgres BIGINT is signed (-2**63, 2**63-1); cast to signed twos-complement
    # so v2 persistence does not overflow.
    if out >= (1 << (bits - 1)):
        out -= 1 << bits
    return out


def hamming_distance(a: int, b: int, bits: int = 64) -> int:
    """Mask to `bits` so signed/unsigned mixing doesn't inflate the distance."""
    mask = (1 << bits) - 1
    return ((a & mask) ^ (b & mask)).bit_count()


@dataclass
class DedupReport:
    total: int = 0
    duplicate_pairs: list[tuple[str, str, int]] = field(default_factory=list)
    duplicate_post_ids: set[str] = field(default_factory=set)
    long_text_fallback_used: int = 0


class PostDeduper:
    """Stateless within a batch; operates in O(n^2) for the in-memory pass.

    For larger ingest batches, push duplicate detection into Postgres via
    `find_simhash_neighbours()` after a first-pass write (Phase 2.9 wiring).
    """

    def __init__(
        self,
        *,
        hamming_threshold: int = 3,
        trgm_threshold: float = 0.85,
        long_text_token_threshold: int = _LONG_TEXT_TOKEN_THRESHOLD,
    ) -> None:
        self._hamming_threshold = hamming_threshold
        self._trgm_threshold = trgm_threshold
        self._long_text_token_threshold = long_text_token_threshold

    def annotate(self, posts: list[Post]) -> None:
        """Compute and attach simhash to each post in `extra` (Pydantic Post
        does not have a native simhash field; attach via attribute for the
        v2 ingest stage to read)."""
        for p in posts:
            try:
                p.simhash = compute_simhash(p.text or "")  # type: ignore[attr-defined]
            except AttributeError:
                # Pydantic v2 disallows arbitrary attributes; stash on
                # `extra` if present, otherwise skip silently.
                pass

    def find_duplicates(
        self,
        posts: list[Post],
        existing: Optional[Iterable[tuple[str, int, str]]] = None,
        long_text_check: Optional[callable] = None,  # type: ignore[name-defined]
    ) -> DedupReport:
        """Return a DedupReport flagging near-duplicate posts.

        Args:
          posts:    incoming batch (will be assigned simhash if missing).
          existing: optional iterable of `(post_id, simhash, text)` from PG
                    for cross-batch comparison.
          long_text_check: optional callable (text_a, text_b) -> similarity
                    used as the long-text fallback. Typically wraps
                    `PostgresService.search_posts_trgm`.
        """
        report = DedupReport(total=len(posts))
        # Annotate any missing simhash
        for p in posts:
            if getattr(p, "simhash", None) is None:
                try:
                    p.simhash = compute_simhash(p.text or "")  # type: ignore[attr-defined]
                except AttributeError:
                    continue

        candidates: list[tuple[str, int, str]] = [
            (p.id, getattr(p, "simhash", 0) or 0, p.text or "")
            for p in posts
        ]
        if existing:
            candidates.extend(existing)

        for i in range(len(candidates)):
            id_a, hash_a, text_a = candidates[i]
            for j in range(i + 1, len(candidates)):
                id_b, hash_b, text_b = candidates[j]
                if hash_a == 0 or hash_b == 0:
                    continue
                dist = hamming_distance(hash_a, hash_b)
                is_dup = dist <= self._hamming_threshold

                # Long-text fallback: simhash unreliable past ~500 tokens
                if (not is_dup
                        and long_text_check is not None
                        and len(tokenize(text_a)) > self._long_text_token_threshold
                        and len(tokenize(text_b)) > self._long_text_token_threshold):
                    sim = float(long_text_check(text_a, text_b) or 0.0)
                    report.long_text_fallback_used += 1
                    if sim >= self._trgm_threshold:
                        is_dup = True
                        dist = -1  # marker meaning "trgm-confirmed"

                if is_dup:
                    report.duplicate_pairs.append((id_a, id_b, dist))
                    report.duplicate_post_ids.add(id_b)  # keep first occurrence

        log.info("post_dedup.done",
                 total=report.total,
                 duplicates=len(report.duplicate_post_ids),
                 pairs=len(report.duplicate_pairs),
                 long_text_fallback=report.long_text_fallback_used)
        return report
