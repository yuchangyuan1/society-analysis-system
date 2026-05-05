"""
Phase 3 (redesign-2026-05) unit tests.

Coverage:
- HybridRetriever: dense + BM25 + RRF fusion + rerank degradation
- NL2SQLTool: SQL whitelist enforcement, repair loop, error lesson recording
- KGQueryTool: four query kinds with mocked Kuzu
- ModuleCard: doc serialisation
- PlannerMemory: branch-combo confidence count
- ReflectionStore: routing rules + ablation hook
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from models.evidence import EvidenceBundle
from models.module_card import ModuleCard, WorkflowExemplar
from models.reflection import CriticVerdict
from tools.hybrid_retrieval import HybridRetriever, _bm25_score_subset
from tools.kg_query_tools import KGQueryTool
from tools.nl2sql_tools import NL2SQLTool, _sanitise_sql


# ── Hybrid retrieval ─────────────────────────────────────────────────────────

def _mock_collections(dense_results: list[dict]):
    cols = MagicMock()
    cols.official.query.return_value = dense_results
    return cols


def _mock_embeddings():
    e = MagicMock()
    e.embed.return_value = [0.1] * 8
    return e


def _mock_reranker(scores=None):
    rr = MagicMock()
    rr.score.return_value = scores or []
    return rr


def test_hybrid_dense_only_when_bm25_unavailable(monkeypatch):
    dense = [
        {"id": "a", "document": "alpha doc", "metadata": {
            "source": "bbc", "domain": "bbc.com", "tier": "reputable_media"}},
        {"id": "b", "document": "beta doc", "metadata": {
            "source": "nyt", "domain": "nytimes.com", "tier": "reputable_media"}},
    ]
    # Force bm25 to be unavailable
    monkeypatch.setattr(
        "tools.hybrid_retrieval._bm25_score_subset",
        lambda docs, q, k: [],
    )
    h = HybridRetriever(
        collections=_mock_collections(dense),
        embeddings=_mock_embeddings(),
        reranker=_mock_reranker([]),
    )
    bundle = h.retrieve("hello")
    assert len(bundle.chunks) == 2
    # Dense rank populated, bm25 None
    assert all(c.dense_rank for c in bundle.chunks)
    assert all(c.bm25_rank is None for c in bundle.chunks)
    assert bundle.rerank_used is False


def test_hybrid_rrf_fuses_dense_and_bm25():
    dense = [
        {"id": f"d{i}", "document": f"doc {i}", "metadata": {
            "source": "x", "domain": "x.com", "tier": "reputable_media"}}
        for i in range(5)
    ]
    h = HybridRetriever(
        collections=_mock_collections(dense),
        embeddings=_mock_embeddings(),
        reranker=_mock_reranker([]),
    )
    # Force BM25 to favour the LAST dense item
    bm25_pairs = [(4, 5.0), (0, 0.1)]
    import tools.hybrid_retrieval as hr
    hr_orig = hr._bm25_score_subset
    hr._bm25_score_subset = lambda docs, q, k: bm25_pairs  # type: ignore[assignment]
    try:
        bundle = h.retrieve("query")
    finally:
        hr._bm25_score_subset = hr_orig  # type: ignore[assignment]
    # Top should be d4 (rank 1 in BM25, rank 5 in dense -> high fused)
    ids = [c.chunk_id for c in bundle.chunks]
    assert "d4" in ids[:2]


def test_hybrid_handles_empty_query():
    h = HybridRetriever(
        collections=_mock_collections([]),
        embeddings=_mock_embeddings(),
        reranker=_mock_reranker(),
    )
    bundle = h.retrieve("")
    assert bundle.chunks == []
    assert "empty_query" in bundle.notes


def test_bm25_scores_subset_no_query_returns_empty():
    docs = [{"document": "alpha"}, {"document": "beta"}]
    out = _bm25_score_subset(docs, "", 10)
    assert out == []


# ── NL2SQL safety ────────────────────────────────────────────────────────────

def test_sanitise_rejects_non_select():
    sql, kind = _sanitise_sql("DROP TABLE posts_v2", row_limit=100)
    assert sql == ""
    assert kind == "sql_syntax"


def test_sanitise_rejects_multi_statement():
    sql, kind = _sanitise_sql("SELECT 1; DROP TABLE x", row_limit=100)
    assert sql == ""
    assert kind == "sql_syntax"


def test_sanitise_adds_limit():
    sql, kind = _sanitise_sql("SELECT id FROM posts_v2", row_limit=42)
    assert kind is None
    assert "LIMIT 42" in sql


def test_sanitise_caps_existing_limit():
    sql, kind = _sanitise_sql("SELECT id FROM posts_v2 LIMIT 9999", row_limit=100)
    assert kind is None
    assert "LIMIT 100" in sql
    assert "LIMIT 9999" not in sql


def test_sanitise_accepts_with_cte():
    sql, kind = _sanitise_sql(
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x", row_limit=10,
    )
    assert kind is None


# ── NL2SQL execution + repair ────────────────────────────────────────────────

def _mock_openai(content: str):
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def test_nl2sql_records_error_lesson_after_repair_exhausted(monkeypatch):
    memory = MagicMock()
    memory.recall_schema.return_value = []
    memory.recall_success.return_value = []
    memory.recall_errors.return_value = []
    memory.upsert_error = MagicMock()

    embeddings = MagicMock()
    embeddings.embed.return_value = [0.0] * 8

    tool = NL2SQLTool(
        memory=memory,
        embeddings=embeddings,
        client=_mock_openai(json.dumps({"sql": "SELECT * FROM nope"})),
        max_repair_rounds=1,
    )
    # Force every execute to raise
    from tools.nl2sql_tools import _SQLExecutionError

    def _raise(self, sql):
        raise _SQLExecutionError("sql_unknown_column", "nope does not exist")

    monkeypatch.setattr(NL2SQLTool, "_execute", _raise)
    out = tool.answer("count posts in nope")
    assert out.success is False
    # repair rounds = 1 -> 2 attempts (initial + 1 repair)
    assert len(out.attempts) == 2
    # Error lesson recorded once per final failure
    memory.upsert_error.assert_called_once()


def test_nl2sql_success_path(monkeypatch):
    memory = MagicMock()
    memory.recall_schema.return_value = [
        {"id": "schema::posts_v2::topic_id",
         "document": "Topic id...", "metadata": {"kind": "schema"}},
    ]
    memory.recall_success.return_value = []
    memory.recall_errors.return_value = []
    embeddings = MagicMock()
    embeddings.embed.return_value = [0.0] * 8

    tool = NL2SQLTool(
        memory=memory,
        embeddings=embeddings,
        client=_mock_openai(json.dumps({
            "sql": "SELECT post_id FROM posts_v2 LIMIT 5",
        })),
    )

    def _ok_execute(self, sql):
        return [{"post_id": "p1"}], ["post_id"]

    monkeypatch.setattr(NL2SQLTool, "_execute", _ok_execute)
    out = tool.answer("show 5 posts")
    assert out.success is True
    assert out.rows == [{"post_id": "p1"}]
    assert out.columns == ["post_id"]
    assert out.attempts[0].error is None


def test_nl2sql_retrieves_chroma2_guidance_without_exposing_it(monkeypatch):
    memory = MagicMock()
    memory.count_guidance.return_value = 5
    memory.recall_guidance.return_value = [{
        "id": "guide::broad_topic_listing",
        "document": "Rule: list topics should not filter to one topic_id.",
        "metadata": {"kind": "guide"},
    }]
    memory.recall_schema.return_value = []
    memory.recall_success.return_value = []
    memory.recall_errors.return_value = []
    embeddings = MagicMock()
    embeddings.embed.return_value = [0.0] * 8
    client = _mock_openai(json.dumps({"sql": "SELECT 1"}))

    tool = NL2SQLTool(
        memory=memory,
        embeddings=embeddings,
        client=client,
    )

    def _ok_execute(self, sql):
        return [{"x": 1}], ["x"]

    monkeypatch.setattr(NL2SQLTool, "_execute", _ok_execute)
    out = tool.answer("List the topics from today's worldnews data")
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert "NL2SQL guidance from Chroma 2" in messages[1]["content"]
    assert "list topics should not filter" in messages[1]["content"]
    assert "used_guidance" not in out.model_dump()


def test_nl2sql_flags_empty_result():
    memory = MagicMock()
    memory.recall_schema.return_value = []
    memory.recall_success.return_value = []
    memory.recall_errors.return_value = []
    embeddings = MagicMock()
    embeddings.embed.return_value = [0.0] * 8

    tool = NL2SQLTool(
        memory=memory,
        embeddings=embeddings,
        client=_mock_openai(json.dumps({
            "sql": "SELECT post_id FROM posts_v2 WHERE 1=0",
        })),
    )

    def _empty(self, sql):
        return [], ["post_id"]

    NL2SQLTool._execute = _empty  # type: ignore[assignment]
    out = tool.answer("impossible query")
    assert out.success is True
    assert out.attempts[-1].error_kind == "sql_empty_result"


# ── KG Query ─────────────────────────────────────────────────────────────────

def _kuzu_mock():
    kuzu = MagicMock()
    kuzu._safe_execute.return_value = []
    return kuzu


def test_kg_propagation_path_returns_paths():
    kuzu = _kuzu_mock()
    # Phase C: propagation_path runs TWO directed Cypher queries (a->b
    # and b->a) and merges. The mock returns nodes(path) raw chains.
    chain = [{"id": "pa1"}, {"id": "pmid"}, {"id": "pb1"}]
    # First call returns a hit, second returns nothing.
    kuzu._safe_execute.side_effect = [
        [{"chain": chain}],
        [],
    ]
    tool = KGQueryTool(kuzu=kuzu)
    out = tool.propagation_path("alice", "bob", max_hops=4)
    assert out.query_kind == "propagation_path"
    assert any(n.id == "alice" and n.label == "Account" for n in out.nodes)
    assert any(n.id == "bob" and n.label == "Account" for n in out.nodes)
    assert any(n.id == "pmid" and n.label == "Post" for n in out.nodes)
    edge_pairs = {(e.source_id, e.target_id) for e in out.edges}
    assert ("pa1", "pmid") in edge_pairs
    assert ("pmid", "pb1") in edge_pairs
    assert out.metrics["paths_found"] == 1
    assert out.metrics["max_path_length"] == 3


def test_kg_key_nodes_returns_top_authors():
    kuzu = _kuzu_mock()
    kuzu._safe_execute.return_value = [
        {"account_id": "u1", "username": "alice", "post_count": 5},
        {"account_id": "u2", "username": "bob", "post_count": 3},
    ]
    out = KGQueryTool(kuzu=kuzu).key_nodes("topic_x", top_k=2)
    assert out.metrics["accounts_in_topic"] == 2
    assert {n.id for n in out.nodes} == {"u1", "u2"}


def test_kg_cascade_tree_collects_descendants():
    kuzu = _kuzu_mock()
    kuzu._safe_execute.return_value = [
        {"post_id": "root", "parent_id": None, "account_id": "alice"},
        {"post_id": "c1",   "parent_id": "root", "account_id": "bob"},
        {"post_id": "c2",   "parent_id": "root", "account_id": "carol"},
        {"post_id": "g1",   "parent_id": "c1",   "account_id": "dave"},
    ]
    out = KGQueryTool(kuzu=kuzu).cascade_tree("root", max_depth=5)
    assert out.metrics["cascade_size"] == 3   # 4 posts - 1 root
    assert out.metrics["unique_authors"] == 4
    assert {(e.source_id, e.target_id) for e in out.edges} == {
        ("c1", "root"), ("c2", "root"), ("g1", "c1"),
    }


def test_kg_viral_cascade_ranks_by_size():
    kuzu = _kuzu_mock()
    kuzu._safe_execute.return_value = [
        {"root_id": "r1", "text": "A", "cascade_size": 5,
         "unique_authors": 3},
        {"root_id": "r2", "text": "B", "cascade_size": 2,
         "unique_authors": 2},
    ]
    out = KGQueryTool(kuzu=kuzu).viral_cascade("topic_x", top_k=5)
    assert out.metrics["cascade_count"] == 2
    assert out.metrics["max_cascade_size"] == 5
    assert out.nodes[0].id == "r1"
    assert out.nodes[0].properties["cascade_size"] == 5


def test_kg_topic_correlation_lists_shared_entities():
    kuzu = _kuzu_mock()
    kuzu._safe_execute.return_value = [
        {"entity_id": "e1", "name": "Vaccine", "entity_type": "OTHER"},
    ]
    out = KGQueryTool(kuzu=kuzu).topic_correlation("a", "b")
    assert out.metrics["shared_entity_count"] == 1
    assert out.nodes[0].properties["name"] == "Vaccine"


def test_kg_falls_back_when_kuzu_missing(monkeypatch):
    # __post_init__ tries to construct a KuzuService when kuzu=None; force
    # that path to fail so we land in the degraded branch.
    import tools.kg_query_tools as mod

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("kuzu unavailable")

    monkeypatch.setattr(mod, "KuzuService", _Boom)
    out = KGQueryTool(kuzu=None).key_nodes("topic_x")
    assert out.nodes == []
    assert out.metrics == {}


# ── ModuleCard / PlannerMemory ───────────────────────────────────────────────

def test_module_card_doc_text_includes_examples():
    card = ModuleCard(
        name="evidence",
        description="d",
        when_to_use=["x"],
        examples=[{"question": "hello?"}],
    )
    text = card.doc_text()
    assert "Branch: evidence" in text
    assert "hello?" in text


def test_planner_memory_count_branch_combo_successes():
    from services.planner_memory import PlannerMemory
    cols = MagicMock()
    cols.planner.handle.get.return_value = {
        "metadatas": [
            {"branches": "evidence,nl2sql"},
            {"branches": "evidence,nl2sql"},
            {"branches": "kg"},
            {"branches": "evidence,nl2sql"},
            {"branches": "evidence"},
            {"branches": "evidence,nl2sql"},
        ],
    }
    pm = PlannerMemory(collections=cols)
    n = pm.count_branch_combo_successes(["evidence", "nl2sql"])
    assert n == 4
    cols.planner.handle.get.assert_called_with(
        where={"kind": "workflow_success"},
        include=["metadatas"],
    )


def test_recall_route_violations_dedups_by_rule_id():
    """Top-N should cover distinct rules first; duplicate rules only fill
    leftover slots after every rule has been represented once.
    """
    from services.planner_memory import PlannerMemory
    cols = MagicMock()
    # 6 records: 3 R-OVERVIEW (most similar), 1 R-KG-TOPIC-ANCHOR,
    # 1 R-FACT-CHECK-EVIDENCE, 1 unrelated kind.
    cols.planner.query.return_value = [
        {"id": "a", "metadata": {"error_kind": "route_violation:R-OVERVIEW"}},
        {"id": "b", "metadata": {"error_kind": "route_violation:R-OVERVIEW"}},
        {"id": "c", "metadata": {"error_kind": "route_violation:R-OVERVIEW"}},
        {"id": "d", "metadata": {"error_kind": "route_violation:R-KG-TOPIC-ANCHOR"}},
        {"id": "e", "metadata": {"error_kind": "route_violation:R-FACT-CHECK-EVIDENCE"}},
        {"id": "f", "metadata": {"error_kind": "missing_branch"}},  # not a violation
    ]
    pm = PlannerMemory(collections=cols)
    out = pm.recall_recent_route_violations([0.0] * 8, n_results=5)

    out_ids = [r["id"] for r in out]
    # Pass 1 picks distinct rules in order: a (overview), d (kg-anchor),
    # e (fact-check). Pass 2 fills with b, c (the remaining overviews).
    assert out_ids[:3] == ["a", "d", "e"]
    assert set(out_ids) == {"a", "b", "c", "d", "e"}
    assert "f" not in out_ids


def test_recall_route_violations_caps_at_n_results():
    from services.planner_memory import PlannerMemory
    cols = MagicMock()
    cols.planner.query.return_value = [
        {"id": f"r{i}", "metadata": {
            "error_kind": f"route_violation:R-{i}",
        }}
        for i in range(10)
    ]
    pm = PlannerMemory(collections=cols)
    out = pm.recall_recent_route_violations([0.0] * 8, n_results=3)
    assert len(out) == 3
    assert [r["id"] for r in out] == ["r0", "r1", "r2"]


# ── ReflectionStore ──────────────────────────────────────────────────────────

def test_reflection_routes_sql_empty_to_nl2sql():
    nl_mem = MagicMock()
    pl_mem = MagicMock()
    emb = MagicMock()
    emb.embed.return_value = [0.0] * 8
    from services.reflection_store import ReflectionStore
    store = ReflectionStore(
        nl2sql_memory=nl_mem, planner_memory=pl_mem, embeddings=emb,
    )
    store.record(
        CriticVerdict(passed=False, error_kind="sql_empty_result",
                      failed_branch="nl2sql"),
        user_message="count posts about vaccines",
    )
    nl_mem.upsert_error.assert_called_once()
    pl_mem.upsert_workflow_error.assert_not_called()


def test_reflection_routes_missing_branch_to_planner():
    nl_mem = MagicMock()
    pl_mem = MagicMock()
    emb = MagicMock()
    emb.embed.return_value = [0.0] * 8
    from services.reflection_store import ReflectionStore
    store = ReflectionStore(
        nl2sql_memory=nl_mem, planner_memory=pl_mem, embeddings=emb,
    )
    store.record(
        CriticVerdict(passed=False, error_kind="missing_branch",
                      failed_branch="planner"),
        user_message="who posted what",
        branches_used=["evidence"],
    )
    pl_mem.upsert_workflow_error.assert_called_once()
    nl_mem.upsert_error.assert_not_called()


def test_reflection_ablation_drops_guilty_record():
    nl_mem = MagicMock()
    pl_mem = MagicMock()
    emb = MagicMock()
    emb.embed.return_value = [0.0] * 8

    def runner(verdict, ids):
        # Pretend the first id is the guilty one
        return ids == ["success::guilty"]

    from services.reflection_store import ReflectionStore
    store = ReflectionStore(
        nl2sql_memory=nl_mem, planner_memory=pl_mem, embeddings=emb,
        ablation_runner=runner,
    )
    store.record(
        CriticVerdict(
            passed=False, error_kind="sql_empty_result",
            failed_branch="nl2sql",
            causal_record_ids=["success::guilty", "success::other"],
        ),
        user_message="bad sql",
    )
    nl_mem.delete_records.assert_called_with(["success::guilty"])


def test_reflection_quarantines_thrashing_record():
    nl_mem = MagicMock()
    pl_mem = MagicMock()
    emb = MagicMock()
    emb.embed.return_value = [0.0] * 8

    from services.reflection_store import ReflectionStore
    store = ReflectionStore(
        nl2sql_memory=nl_mem, planner_memory=pl_mem, embeddings=emb,
        ablation_runner=lambda v, ids: True,
    )
    for _ in range(4):
        store.record(
            CriticVerdict(
                passed=False, error_kind="sql_empty_result",
                failed_branch="nl2sql",
                causal_record_ids=["success::looper"],
            ),
            user_message="repeating",
        )
    # 2 deletes allowed, then quarantined
    assert nl_mem.delete_records.call_count <= 2


def test_reflection_passed_verdict_is_audit_only():
    nl_mem = MagicMock()
    pl_mem = MagicMock()
    emb = MagicMock()
    emb.embed.return_value = [0.0] * 8
    from services.reflection_store import ReflectionStore
    store = ReflectionStore(
        nl2sql_memory=nl_mem, planner_memory=pl_mem, embeddings=emb,
    )
    store.record(
        CriticVerdict(passed=True),
        user_message="all good",
    )
    nl_mem.upsert_error.assert_not_called()
    pl_mem.upsert_workflow_error.assert_not_called()
