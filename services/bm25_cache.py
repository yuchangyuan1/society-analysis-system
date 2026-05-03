"""
BM25 index cache - production hardening Day 1.

`tools/hybrid_retrieval.HybridRetriever._bm25_score_subset` previously
rebuilt a BM25Okapi index on every retrieval call. With 169 chunks that's
~150ms; at 5K-10K chunks the rebuild dominates the latency budget.

This module memoises BM25Okapi instances keyed by the corpus content
fingerprint. The fingerprint is derived from the sorted list of chunk
ids in the candidate set, so any change to membership (new ingestion,
chunk deletion, metadata-filter change) automatically invalidates.

For cross-process invalidation we expose `bump_corpus_version()` which
the OfficialIngestionPipeline calls after writing new chunks. That moves
a process-wide counter so even cached entries built from "the same ids"
are considered stale after a write.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)

_lock = threading.Lock()
_corpus_version: int = 0


def bump_corpus_version() -> int:
    """Invalidate every cached BM25 index. Called after Chroma 1 writes."""
    global _corpus_version
    with _lock:
        _corpus_version += 1
        return _corpus_version


def current_corpus_version() -> int:
    return _corpus_version


def fingerprint_corpus(chunk_ids: list[str]) -> str:
    """Stable hash of an ordered chunk-id list (corpus identity)."""
    h = hashlib.sha256()
    for cid in sorted(chunk_ids):
        h.update(cid.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


@dataclass
class _Entry:
    bm25: Any                  # BM25Okapi instance
    version: int               # corpus_version at insertion time


class BM25Cache:
    """LRU-bounded cache of BM25Okapi indexes.

    Key shape: (corpus_fingerprint, lang). Lang is reserved for future
    multilingual tokenisers; today it's always "en".
    """

    def __init__(self, capacity: int = 4) -> None:
        self._cap = max(1, capacity)
        self._data: "OrderedDict[tuple, _Entry]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, corpus_fingerprint: str, lang: str = "en") -> Optional[Any]:
        key = (corpus_fingerprint, lang)
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.version != _corpus_version:
                # Global write happened after we cached this; drop it.
                self._data.pop(key, None)
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return entry.bm25

    def put(self, corpus_fingerprint: str, bm25: Any,
             lang: str = "en") -> None:
        key = (corpus_fingerprint, lang)
        with self._lock:
            self._data[key] = _Entry(bm25=bm25, version=_corpus_version)
            self._data.move_to_end(key)
            while len(self._data) > self._cap:
                self._data.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits, "misses": self._misses,
                "size": len(self._data),
                "corpus_version": _corpus_version,
            }

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# Module-level singleton consumed by HybridRetriever.
BM25_CACHE = BM25Cache(capacity=4)
