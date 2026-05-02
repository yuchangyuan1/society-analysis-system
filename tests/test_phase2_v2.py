"""
Phase 2 (redesign-2026-05) unit tests.

Coverage:
- SchemaProposal fingerprint stability
- SchemaAgent core columns presence
- NL2SQLMemory three-tier conflict policy
- PostDeduper simhash + Hamming threshold
- TopicClusterer cluster + assignment
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.entity_extractor import EntityExtractor  # noqa: F401
from agents.post_dedup import (
    PostDeduper,
    compute_simhash,
    hamming_distance,
)
from agents.schema_agent import SchemaAgent
from agents.topic_clusterer import TopicClusterer, TopicCluster
from models.post import Post
from models.schema_proposal import ColumnSpec, SchemaProposal
from services.nl2sql_memory import NL2SQLMemory


# ── SchemaProposal ───────────────────────────────────────────────────────────

def test_schema_proposal_fingerprint_stable_across_orderings():
    a = SchemaProposal(run_id="r")
    a.columns = [
        ColumnSpec(table_name="posts_v2", column_name="x", column_type="TEXT",
                   description="x"),
        ColumnSpec(table_name="posts_v2", column_name="y", column_type="REAL",
                   description="y"),
    ]
    b = SchemaProposal(run_id="r")
    b.columns = list(reversed(a.columns))
    assert a.schema_fingerprint() == b.schema_fingerprint()


def test_schema_proposal_fingerprint_changes_when_column_changes():
    a = SchemaProposal(run_id="r")
    a.columns = [ColumnSpec(table_name="posts_v2", column_name="x",
                            column_type="TEXT", description="x")]
    b = SchemaProposal(run_id="r")
    b.columns = [ColumnSpec(table_name="posts_v2", column_name="x",
                            column_type="INTEGER", description="x")]
    assert a.schema_fingerprint() != b.schema_fingerprint()


def test_column_spec_location_filter():
    proposal = SchemaProposal(run_id="r")
    proposal.columns = [
        ColumnSpec(table_name="posts_v2", column_name="post_id",
                   column_type="TEXT", description="d", location="core"),
        ColumnSpec(table_name="posts_v2", column_name="vote_ratio",
                   column_type="REAL", description="d", location="extra"),
    ]
    assert {c.column_name for c in proposal.core_columns()} == {"post_id"}
    assert {c.column_name for c in proposal.extra_columns()} == {"vote_ratio"}


# ── SchemaAgent ──────────────────────────────────────────────────────────────

def _mock_openai_response(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_schema_agent_includes_all_core_columns():
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps({"fields": []}),
    )
    agent = SchemaAgent(client=client)
    proposal = agent.propose(
        run_id="r1",
        posts=[Post(id="p1", account_id="u", text="hello")],
    )
    core_names = {c.column_name for c in proposal.core_columns()}
    # Must include the fixed core set
    assert "post_id" in core_names
    assert "topic_id" in core_names
    assert "simhash" in core_names
    assert "extra" in core_names


def test_schema_agent_extra_fields_normalised():
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        json.dumps({"fields": [
            {"name": "vote_ratio", "type": "float",
             "description": "Upvote ratio."},
            {"name": "tags", "type": "array", "description": "Hashtag list."},
        ]})
    )
    agent = SchemaAgent(client=client)
    proposal = agent.propose(
        run_id="r1",
        posts=[Post(id="p1", account_id="u", text="hello")],
    )
    extras = {c.column_name: c.column_type for c in proposal.extra_columns()}
    assert extras == {"vote_ratio": "REAL", "tags": "JSONB"}


def test_schema_agent_handles_empty_posts():
    client = MagicMock()
    agent = SchemaAgent(client=client)
    proposal = agent.propose(run_id="r1", posts=[])
    # Core still present even when LLM is not called
    assert any(c.column_name == "post_id" for c in proposal.columns)
    client.chat.completions.create.assert_not_called()


# ── NL2SQLMemory conflict policy ─────────────────────────────────────────────

def _build_memory_with_query_results(
    query_results: list[dict],
) -> tuple[NL2SQLMemory, MagicMock]:
    """Build an NL2SQLMemory with a mocked Chroma collections facade."""
    cols = MagicMock()
    cols.nl2sql.query.return_value = query_results
    cols.nl2sql.upsert = MagicMock()
    cols.nl2sql.delete = MagicMock()
    cols.nl2sql.handle = MagicMock()
    memory = NL2SQLMemory(collections=cols, sim_low=0.92, sim_high=0.95)
    return memory, cols


def test_nl2sql_memory_appends_when_low_similarity():
    memory, cols = _build_memory_with_query_results([
        {"id": "old", "document": "old text", "similarity": 0.5,
         "metadata": {"kind": "success"}}
    ])
    memory.upsert_success("nl", "SQL", embedding=[0.1] * 8)
    cols.nl2sql.delete.assert_not_called()
    assert cols.nl2sql.upsert.call_count == 1


def test_nl2sql_memory_replaces_directly_in_mid_band():
    memory, cols = _build_memory_with_query_results([
        {"id": "old", "document": "old text", "similarity": 0.93,
         "metadata": {"kind": "success"}}
    ])
    memory.upsert_success("nl", "SQL", embedding=[0.1] * 8)
    cols.nl2sql.delete.assert_called_once_with(ids=["old"])
    assert cols.nl2sql.upsert.call_count == 1


def test_nl2sql_memory_uses_llm_judge_above_high():
    judge_calls = []

    def judge(new_text: str, old_text: str) -> bool:
        judge_calls.append((new_text, old_text))
        return True

    cols = MagicMock()
    cols.nl2sql.query.return_value = [{
        "id": "old", "document": "old text", "similarity": 0.97,
        "metadata": {"kind": "success"},
    }]
    cols.nl2sql.handle = MagicMock()
    memory = NL2SQLMemory(
        collections=cols, sim_low=0.92, sim_high=0.95, llm_judge=judge,
    )
    memory.upsert_success("nl", "SQL", embedding=[0.1] * 8)
    assert len(judge_calls) == 1
    cols.nl2sql.delete.assert_called_once_with(ids=["old"])


def test_nl2sql_memory_llm_judge_says_no_conflict():
    cols = MagicMock()
    cols.nl2sql.query.return_value = [{
        "id": "old", "document": "old text", "similarity": 0.97,
        "metadata": {"kind": "success"},
    }]
    cols.nl2sql.handle = MagicMock()
    memory = NL2SQLMemory(
        collections=cols, sim_low=0.92, sim_high=0.95,
        llm_judge=lambda new, old: False,
    )
    memory.upsert_success("nl", "SQL", embedding=[0.1] * 8)
    cols.nl2sql.delete.assert_not_called()
    assert cols.nl2sql.upsert.call_count == 1


def test_nl2sql_memory_schema_uses_deterministic_id():
    cols = MagicMock()
    cols.nl2sql.upsert = MagicMock()
    cols.nl2sql.handle = MagicMock()
    memory = NL2SQLMemory(collections=cols)
    rid = memory.upsert_schema(
        table_name="posts_v2",
        column_name="post_id",
        text="post id",
        embedding=[0.0] * 8,
        fingerprint="fp",
    )
    assert rid == "schema::posts_v2::post_id"
    args, kwargs = cols.nl2sql.upsert.call_args
    assert kwargs["ids"] == ["schema::posts_v2::post_id"]
    assert kwargs["metadatas"][0]["kind"] == "schema"
    assert kwargs["metadatas"][0]["fingerprint"] == "fp"


# ── PostDeduper ──────────────────────────────────────────────────────────────

def test_simhash_identical_texts_have_zero_distance():
    a = compute_simhash("the quick brown fox jumps over the lazy dog")
    b = compute_simhash("the quick brown fox jumps over the lazy dog")
    assert a == b
    assert hamming_distance(a, b) == 0


def test_simhash_unrelated_texts_have_large_distance():
    a = compute_simhash("the quick brown fox jumps over the lazy dog")
    b = compute_simhash("artificial intelligence transforms modern industries")
    assert hamming_distance(a, b) > 10


def test_post_deduper_flags_near_duplicates():
    text_a = "Breaking news: vaccines reduce hospitalisation by 90 percent"
    text_b = "Breaking news: vaccines reduce hospitalisation by 90 percent."
    posts = [
        Post(id="p1", account_id="u1", text=text_a),
        Post(id="p2", account_id="u2", text=text_b),
        Post(id="p3", account_id="u3",
             text="Completely different topic about climate change models"),
    ]
    deduper = PostDeduper(hamming_threshold=3)
    report = deduper.find_duplicates(posts)
    assert "p2" in report.duplicate_post_ids
    assert "p3" not in report.duplicate_post_ids


def test_post_deduper_long_text_fallback_invoked():
    long_a = " ".join([f"token{i}" for i in range(600)])
    long_b = " ".join([f"token{i}" for i in range(600)] + ["minor", "diff"])
    calls = []

    def fake_trgm(a: str, b: str) -> float:
        calls.append((a[:20], b[:20]))
        return 0.95

    posts = [
        Post(id="p1", account_id="u", text=long_a),
        Post(id="p2", account_id="u", text=long_b),
    ]
    deduper = PostDeduper(hamming_threshold=0, trgm_threshold=0.85)
    report = deduper.find_duplicates(posts, long_text_check=fake_trgm)
    assert calls, "long-text fallback should fire"
    assert "p2" in report.duplicate_post_ids


# ── TopicClusterer ───────────────────────────────────────────────────────────

def test_topic_clusterer_assigns_topic_ids():
    embeddings = MagicMock()

    def fake_embed_batch(texts):
        out = []
        for t in texts:
            if "vaccine" in t.lower():
                out.append([1.0, 0.0])
            elif "climate" in t.lower():
                out.append([0.0, 1.0])
            else:
                out.append([0.5, 0.5])
        return out

    embeddings.embed_batch.side_effect = fake_embed_batch

    posts = [
        Post(id=f"p{i}", account_id="u",
             text=f"vaccine update {i}") for i in range(3)
    ] + [
        Post(id=f"q{i}", account_id="u",
             text=f"climate report {i}") for i in range(3)
    ]
    clusterer = TopicClusterer(
        embeddings=embeddings, min_posts=2, target_per_cluster=3,
        min_clusters=2, max_clusters=2,
    )
    clusters = clusterer.cluster(posts)
    pytest.importorskip("sklearn")
    assert len(clusters) == 2
    # All posts get a topic_id
    assert all(p.topic_id is not None for p in posts)
    # Vaccine posts share a topic; climate posts share a topic
    vaccine_topics = {p.topic_id for p in posts[:3]}
    climate_topics = {p.topic_id for p in posts[3:]}
    assert len(vaccine_topics) == 1
    assert len(climate_topics) == 1
    assert vaccine_topics != climate_topics


def test_topic_clusterer_skip_when_too_few_posts():
    clusterer = TopicClusterer(min_posts=10)
    clusters = clusterer.cluster([
        Post(id="p1", account_id="u", text="hello"),
    ])
    assert clusters == []


# ── Pipeline v2 wiring (Phase 2 stages) ──────────────────────────────────────

def test_pipeline_v2_phase2_stages_run(tmp_path, monkeypatch):
    from agents.precompute_pipeline_v2 import PrecomputePipelineV2

    posts = [
        Post(id="p1", account_id="u1", text="hello world"),
        Post(id="p2", account_id="u2", text="another post"),
    ]
    ingestion = MagicMock()
    ingestion.ingest_posts_from_jsonl.return_value = posts
    knowledge = MagicMock()

    schema_agent = MagicMock()
    schema_agent.propose.return_value = SchemaProposal(run_id="run-1")
    schema_sync = MagicMock()
    pg = MagicMock()
    pg.upsert_post_v2 = MagicMock()
    pg.upsert_topic_v2 = MagicMock()

    pipeline = PrecomputePipelineV2(
        ingestion=ingestion,
        knowledge=knowledge,
        multimodal=MagicMock(enrich_posts=MagicMock()),
        entity_extractor=MagicMock(extract_for_posts=MagicMock()),
        topic_clusterer=MagicMock(cluster=MagicMock(return_value=[])),
        post_deduper=MagicMock(
            annotate=MagicMock(),
            find_duplicates=MagicMock(return_value=MagicMock(
                duplicate_post_ids=set(),
            )),
        ),
        schema_agent=schema_agent,
        schema_sync=schema_sync,
        pg=pg,
        kuzu=None,
    )
    result = pipeline.run(run_dir=tmp_path / "r1", jsonl_path="any.jsonl")
    stage_names = [s.name for s in result.stages]
    assert "schema_propose" in stage_names
    assert "persist_v2" in stage_names
    schema_sync.apply_proposal.assert_called_once()
    assert pg.upsert_post_v2.call_count == 2
