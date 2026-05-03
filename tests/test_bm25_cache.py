"""
BM25 cache tests - production hardening Day 1.

Verifies:
  - Cache hit on identical chunk-id set
  - Cache miss when chunk-id set changes (different fingerprint)
  - bump_corpus_version() invalidates everything
  - LRU eviction when capacity exceeded
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from services.bm25_cache import (
    BM25_CACHE,
    BM25Cache,
    bump_corpus_version,
    fingerprint_corpus,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    BM25_CACHE.clear()
    bump_corpus_version()
    yield
    BM25_CACHE.clear()


def test_fingerprint_is_order_independent():
    a = fingerprint_corpus(["c1", "c2", "c3"])
    b = fingerprint_corpus(["c3", "c1", "c2"])
    assert a == b


def test_fingerprint_changes_when_membership_changes():
    a = fingerprint_corpus(["c1", "c2"])
    b = fingerprint_corpus(["c1", "c2", "c3"])
    assert a != b


def test_bm25_score_subset_uses_cache(monkeypatch):
    from tools import hybrid_retrieval as hr

    build_count = {"n": 0}

    class FakeBM25:
        def __init__(self, corpus):
            build_count["n"] += 1
            self._corpus = corpus

        def get_scores(self, q):
            return [1.0] * len(self._corpus)

    # Patch the lazy import inside _bm25_score_subset
    fake_module = type("M", (), {"BM25Okapi": FakeBM25})
    monkeypatch.setitem(__import__("sys").modules, "rank_bm25", fake_module)

    docs = [
        {"id": "c1", "document": "alpha bravo"},
        {"id": "c2", "document": "charlie delta"},
    ]
    # First call: builds index
    hr._bm25_score_subset(docs, "alpha", top_k=2)
    assert build_count["n"] == 1

    # Second call same docs: should hit cache
    hr._bm25_score_subset(docs, "delta", top_k=2)
    assert build_count["n"] == 1

    # Third call different doc-id set: should rebuild
    hr._bm25_score_subset(
        docs + [{"id": "c3", "document": "echo foxtrot"}],
        "echo", top_k=2,
    )
    assert build_count["n"] == 2


def test_bump_corpus_version_invalidates_cache(monkeypatch):
    from tools import hybrid_retrieval as hr

    build_count = {"n": 0}

    class FakeBM25:
        def __init__(self, corpus):
            build_count["n"] += 1

        def get_scores(self, q):
            return [0.5]

    monkeypatch.setitem(
        __import__("sys").modules,
        "rank_bm25",
        type("M", (), {"BM25Okapi": FakeBM25}),
    )

    docs = [{"id": "c1", "document": "alpha"}]
    hr._bm25_score_subset(docs, "alpha", top_k=1)
    hr._bm25_score_subset(docs, "alpha", top_k=1)
    assert build_count["n"] == 1  # cached

    bump_corpus_version()
    hr._bm25_score_subset(docs, "alpha", top_k=1)
    assert build_count["n"] == 2  # invalidated -> rebuild


def test_lru_eviction():
    cache = BM25Cache(capacity=2)
    cache.put("fp_a", object())
    cache.put("fp_b", object())
    cache.put("fp_c", object())  # forces eviction of fp_a (oldest)
    assert cache.get("fp_a") is None
    assert cache.get("fp_b") is not None
    assert cache.get("fp_c") is not None


def test_stats_track_hits_and_misses():
    cache = BM25Cache(capacity=4)
    cache.get("missing")  # miss
    cache.get("missing")  # miss
    cache.put("present", object())
    cache.get("present")  # hit
    s = cache.stats()
    assert s["hits"] == 1
    assert s["misses"] == 2


def test_official_ingestion_bumps_corpus_version():
    """Smoke: OfficialIngestionPipeline.upsert path bumps the version."""
    from unittest.mock import MagicMock
    from agents.official_ingestion_pipeline import (
        OfficialIngestionPipeline,
        PipelineConfig,
    )
    from models.official_chunk import OfficialChunk
    from datetime import datetime
    import services.bm25_cache as bm25_cache_mod

    chroma = MagicMock()
    chroma.official.upsert = MagicMock()

    embeddings = MagicMock()
    embeddings.embed_batch.return_value = [[0.0] * 4]

    p = OfficialIngestionPipeline(
        cfg=PipelineConfig(),
        news_service=None,
        embeddings=embeddings,
        chroma=chroma,
        write_chroma=True,
    )
    seq_before = bm25_cache_mod.current_corpus_version()
    p._upsert_to_chroma([
        OfficialChunk(
            chunk_id="c1", source="bbc", domain="bbc.com",
            tier="reputable_media", url="u", title="t",
            chunk_index=0, text="text", token_count=1,
            publish_date=datetime(2026, 5, 3),
        ),
    ])
    seq_after = bm25_cache_mod.current_corpus_version()
    assert seq_after > seq_before
