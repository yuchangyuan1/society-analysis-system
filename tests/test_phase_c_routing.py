"""
Phase C - Planner KG routing tests.

Verifies that:
  - Each new SubtaskIntent maps to the right branch combination.
  - default_kg_runner dispatches each intent to the correct KG method.
  - PrecomputePipelineV2.persist_v2 bumps the KG write sequence so the
    cache invalidates after fresh data lands in Kuzu.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.planner_v2 import (
    BoundedPlannerV2,
    BranchInvocation,
    default_kg_runner,
)
from models.branch_output import KGOutput
from models.query import RewrittenQuery, Subtask, SubtaskTarget


# ── Branch routing per intent ────────────────────────────────────────────────

@pytest.mark.parametrize("intent, expected", [
    ("propagation_trace",   ["kg"]),
    ("influencer_query",    ["kg", "nl2sql"]),
    ("coordination_check",  ["kg"]),
    ("community_structure", ["kg", "nl2sql"]),
    ("cascade_query",       ["kg"]),
])
def test_planner_routes_kg_intents_to_kg(intent, expected):
    rq = RewrittenQuery(
        original="x",
        subtasks=[Subtask(text="t", intent=intent)],
    )
    planner = BoundedPlannerV2(
        evidence_runner=lambda inv: None,
        nl2sql_runner=lambda inv: None,
        kg_runner=lambda inv: None,
    )
    plan = planner._plan(rq)
    branches = [inv.branch for inv in plan]
    assert branches == expected


def test_planner_passes_propagation_trace_endpoints_via_metadata_filter():
    rq = RewrittenQuery(
        original="x",
        subtasks=[Subtask(
            text="trace alice to dave",
            intent="propagation_trace",
            targets=SubtaskTarget(metadata_filter={
                "source_account": "alice",
                "target_account": "dave",
            }),
        )],
    )
    planner = BoundedPlannerV2(
        evidence_runner=lambda inv: None,
        nl2sql_runner=lambda inv: None,
        kg_runner=lambda inv: None,
    )
    plan = planner._plan(rq)
    kg_inv = next(inv for inv in plan if inv.branch == "kg")
    assert kg_inv.payload["intent"] == "propagation_trace"
    assert kg_inv.payload["source_account"] == "alice"
    assert kg_inv.payload["target_account"] == "dave"


# ── default_kg_runner dispatch ───────────────────────────────────────────────

def _kg_out(kind: str = "stub") -> KGOutput:
    return KGOutput(query_kind=kind, target={})  # type: ignore[arg-type]


def test_kg_runner_propagation_trace_calls_propagation_path():
    inv = BranchInvocation(
        subtask_index=0, branch="kg",
        payload={
            "intent": "propagation_trace",
            "source_account": "alice", "target_account": "dave",
        },
    )
    with patch("tools.kg_query_tools.KGQueryTool") as KGT:
        instance = KGT.return_value
        instance.propagation_path.return_value = _kg_out("propagation_path")
        default_kg_runner(inv)
        instance.propagation_path.assert_called_once_with(
            source_account="alice", target_account="dave", max_hops=6,
        )


def test_kg_runner_propagation_trace_returns_safe_default_when_endpoints_missing():
    inv = BranchInvocation(
        subtask_index=0, branch="kg",
        payload={"intent": "propagation_trace"},
    )
    out = default_kg_runner(inv)
    assert out.query_kind == "propagation_path"
    assert "missing" in (out.target.get("reason") or "")


def test_kg_runner_cascade_query_calls_viral_cascade():
    inv = BranchInvocation(
        subtask_index=0, branch="kg",
        payload={"intent": "cascade_query", "topic_id": "t1"},
    )
    with patch("tools.kg_query_tools.KGQueryTool") as KGT:
        instance = KGT.return_value
        instance.viral_cascade.return_value = _kg_out("viral_cascade")
        default_kg_runner(inv)
        instance.viral_cascade.assert_called_once_with(
            topic_id="t1", top_k=5,
        )


def test_kg_runner_influencer_query_calls_pagerank():
    inv = BranchInvocation(
        subtask_index=0, branch="kg",
        payload={"intent": "influencer_query", "topic_id": "t1"},
    )
    with patch("agents.kg_analytics.KGAnalytics") as KGA:
        instance = KGA.return_value
        instance.influencer_rank.return_value = _kg_out("influencer_rank")
        default_kg_runner(inv)
        instance.influencer_rank.assert_called_once()
        kwargs = instance.influencer_rank.call_args.kwargs
        assert kwargs["topic_id"] == "t1"


def test_kg_runner_coordination_check_calls_louvain():
    inv = BranchInvocation(
        subtask_index=0, branch="kg",
        payload={"intent": "coordination_check", "topic_id": "t1"},
    )
    with patch("agents.kg_analytics.KGAnalytics") as KGA:
        instance = KGA.return_value
        instance.coordinated_groups.return_value = _kg_out("coordinated_groups")
        default_kg_runner(inv)
        instance.coordinated_groups.assert_called_once()


def test_kg_runner_community_structure_calls_echo_chamber():
    inv = BranchInvocation(
        subtask_index=0, branch="kg",
        payload={"intent": "community_structure", "topic_id": "t1"},
    )
    with patch("agents.kg_analytics.KGAnalytics") as KGA:
        instance = KGA.return_value
        instance.echo_chamber.return_value = _kg_out("echo_chamber")
        default_kg_runner(inv)
        instance.echo_chamber.assert_called_once()


# ── pipeline bump_write_seq ─────────────────────────────────────────────────

def test_pipeline_persist_v2_bumps_kg_write_seq(tmp_path):
    from agents.precompute_pipeline_v2 import PrecomputePipelineV2
    from models.post import Post
    from services.kg_cache import current_write_seq

    posts = [Post(id="p1", account_id="a", text="hi")]
    ingestion = MagicMock()
    ingestion.ingest_posts_from_jsonl.return_value = posts
    knowledge = MagicMock()

    pg = MagicMock()
    kuzu = MagicMock()

    pipeline = PrecomputePipelineV2(
        ingestion=ingestion, knowledge=knowledge,
        multimodal=MagicMock(enrich_posts=MagicMock()),
        entity_extractor=MagicMock(extract_for_posts=MagicMock()),
        topic_clusterer=MagicMock(cluster=MagicMock(return_value=[])),
        post_deduper=MagicMock(
            annotate=MagicMock(),
            find_duplicates=MagicMock(return_value=MagicMock(
                duplicate_post_ids=set(),
            )),
        ),
        schema_agent=None, schema_sync=None,
        pg=pg, kuzu=kuzu,
    )
    seq_before = current_write_seq()
    pipeline.run(run_dir=tmp_path / "kg-bump-run", jsonl_path="any.jsonl")
    seq_after = current_write_seq()
    assert seq_after > seq_before
