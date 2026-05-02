"""
KG subgraph cache - redesign-2026-05-kg Phase B.4.

KG analytics (PageRank / Louvain / betweenness) are expensive and often
share the same input subgraph (e.g. influencer_rank and bridge_accounts
both want "all reply edges + all post edges in topic T"). This module
keeps a small LRU cache keyed by (topic_id, kuzu_write_seq) so
back-to-back algorithms don't re-pull from Kuzu.

Cache invalidation: every time the v2 pipeline writes a new run, it
should bump the global write sequence via `bump_write_seq()`. The cache
key includes that sequence so older subgraphs naturally expire when
underlying data changes.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)

_lock = threading.Lock()
_write_seq: int = 0


def bump_write_seq() -> int:
    """Invalidate all cached subgraphs. Call after every Kuzu write batch."""
    global _write_seq
    with _lock:
        _write_seq += 1
        return _write_seq


def current_write_seq() -> int:
    return _write_seq


@dataclass
class _Entry:
    payload: Any
    seq: int


class LRUSubgraphCache:
    """Simple OrderedDict-backed LRU.

    `payload` is whatever the analytics module wants to memoise — typically
    a NetworkX DiGraph plus a node-attribute dict.
    """

    def __init__(self, capacity: int = 8) -> None:
        self._cap = max(1, capacity)
        self._data: "OrderedDict[tuple, _Entry]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: tuple) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            # Stale (data changed since cache populated)
            if entry.seq != _write_seq:
                self._data.pop(key, None)
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return entry.payload

    def put(self, key: tuple, payload: Any) -> None:
        with self._lock:
            self._data[key] = _Entry(payload=payload, seq=_write_seq)
            self._data.move_to_end(key)
            while len(self._data) > self._cap:
                self._data.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses,
                    "size": len(self._data),
                    "write_seq": _write_seq}

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# Module-level shared cache; algorithms in agents/kg_analytics.py reuse this.
SUBGRAPH_CACHE = LRUSubgraphCache(capacity=8)
