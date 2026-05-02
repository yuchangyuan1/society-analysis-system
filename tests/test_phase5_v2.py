"""
Phase 5 (redesign-2026-05) unit tests.

Coverage:
- decay_chroma_experience.sweep_collection: TTL + confidence rules + anchor
  preservation + dry-run
- AblationRunner: muted recall + replay + restore
- Reflection API plumbing (route handlers smoke-tested via FastAPI TestClient)
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agents.ablation_runner import AblationContext, AblationRunner
from agents.planner_v2 import (
    BoundedPlannerV2,
    BranchInvocation,
    BranchResult,
    PlanExecutionV2,
)
from models.branch_output import BranchExecutionStatus, EvidenceOutput
from models.evidence import Citation, EvidenceBundle, EvidenceChunk
from models.query import RewrittenQuery, Subtask
from models.reflection import CriticVerdict
from models.report_v2 import ReportV2


# ── decay_chroma_experience ──────────────────────────────────────────────────

def _stub_collection(metadatas: list[dict]):
    """Build a minimal _CollectionWrapper-ish stub."""
    handle = MagicMock()
    handle.handle.get.return_value = {
        "ids": [f"id{i}" for i in range(len(metadatas))],
        "metadatas": metadatas,
    }
    deleted: list[list[str]] = []
    handle.delete.side_effect = lambda ids=None, where=None: deleted.append(ids or [])
    handle._deleted = deleted
    return handle


def test_sweep_collection_drops_stale_records():
    from scripts.decay_chroma_experience import sweep_collection

    now = time.time()
    handle = _stub_collection([
        {"kind": "schema", "last_used_at": now - 10 * 24 * 3600,
         "confidence": 0.1},
        {"kind": "success", "last_used_at": now - 31 * 24 * 3600,
         "confidence": 0.5},
        {"kind": "error", "last_used_at": now - 1 * 24 * 3600,
         "confidence": 0.05},
        {"kind": "workflow_success", "last_used_at": now - 1 * 24 * 3600,
         "confidence": 0.6},
    ])
    result = sweep_collection(
        "nl2sql", handle, ttl_days=30, min_confidence=0.2, dry_run=False,
    )
    assert result["to_drop"] == 2
    assert result["deleted"] == 2
    # Anchor (kind=schema) skipped despite being stale
    assert result["breakdown"]["anchor_skipped"] == 1
    assert result["breakdown"]["ttl"] == 1
    assert result["breakdown"]["low_confidence"] == 1
    assert result["breakdown"]["kept"] == 1
    # Verify deletion ids correspond to id1 + id2 (skipping id0 schema, id3 kept)
    deleted_calls = [call for call in handle._deleted if call]
    assert deleted_calls == [["id1", "id2"]]


def test_sweep_collection_dry_run_writes_nothing():
    from scripts.decay_chroma_experience import sweep_collection

    handle = _stub_collection([
        {"kind": "success",
         "last_used_at": time.time() - 90 * 24 * 3600,
         "confidence": 0.5},
    ])
    result = sweep_collection(
        "nl2sql", handle, ttl_days=30, min_confidence=0.2, dry_run=True,
    )
    assert result["to_drop"] == 1
    assert result["deleted"] == 0
    assert handle._deleted == []


def test_sweep_collection_skips_unknown_kind():
    from scripts.decay_chroma_experience import sweep_collection

    handle = _stub_collection([
        {"kind": "module_card", "last_used_at": 0, "confidence": 0.0},
        {"kind": "weird_kind", "last_used_at": 0, "confidence": 0.0},
    ])
    result = sweep_collection(
        "planner", handle, ttl_days=30, min_confidence=0.2, dry_run=True,
    )
    assert result["to_drop"] == 0
    assert result["breakdown"]["anchor_skipped"] == 1
    assert result["breakdown"]["no_kind"] == 1


# ── AblationRunner ───────────────────────────────────────────────────────────

def _exec_with_evidence():
    bundle = EvidenceBundle(query="q", chunks=[EvidenceChunk(
        chunk_id="c1", text="x",
        citation=Citation(chunk_id="c1", source="bbc", domain="bbc.com"))])
    inv = BranchInvocation(subtask_index=0, branch="evidence", payload={})
    return PlanExecutionV2(
        workflow=[inv],
        results=[BranchResult(
            invocation=inv,
            status=BranchExecutionStatus(branch="evidence", success=True),
            output=EvidenceOutput(bundle=bundle).model_dump(),
        )],
        branches_used=["evidence"],
    )


def test_ablation_runner_returns_true_when_replay_passes():
    nl_memory = MagicMock()
    nl_memory.recall_schema = MagicMock(return_value=[
        {"id": "schema::posts_v2::topic_id", "document": "x"},
        {"id": "success::guilty", "document": "y"},
    ])
    nl_memory.recall_success = MagicMock(return_value=[])
    nl_memory.recall_errors = MagicMock(return_value=[])
    planner_memory = MagicMock()
    planner_memory.recall_module_cards = MagicMock(return_value=[])
    planner_memory.recall_workflow_exemplars = MagicMock(return_value=[])
    planner_memory.recall_workflow_errors = MagicMock(return_value=[])

    planner = MagicMock()
    planner.plan_and_execute.return_value = _exec_with_evidence()
    writer = MagicMock()
    writer.write.return_value = ReportV2(user_question="q",
                                          markdown_body="ok")
    critic = MagicMock()
    # Replay passes -> guilty
    critic.review.return_value = CriticVerdict(passed=True)

    rq = RewrittenQuery(original="q",
                         subtasks=[Subtask(text="q", intent="freeform")])
    ctx = AblationContext(
        rewritten=rq, execution=_exec_with_evidence(),
        planner=planner, writer=writer, critic=critic,
        nl2sql_memory=nl_memory, planner_memory=planner_memory,
    )
    runner = AblationRunner(context=ctx)
    verdict = CriticVerdict(passed=False, error_kind="sql_empty_result",
                             causal_record_ids=["success::guilty"])
    assert runner(verdict, ["success::guilty"]) is True


def test_ablation_runner_returns_false_when_replay_still_fails():
    nl_memory = MagicMock()
    nl_memory.recall_schema = MagicMock(return_value=[])
    nl_memory.recall_success = MagicMock(return_value=[])
    nl_memory.recall_errors = MagicMock(return_value=[])
    planner_memory = MagicMock()
    planner_memory.recall_module_cards = MagicMock(return_value=[])
    planner_memory.recall_workflow_exemplars = MagicMock(return_value=[])
    planner_memory.recall_workflow_errors = MagicMock(return_value=[])

    planner = MagicMock()
    planner.plan_and_execute.return_value = _exec_with_evidence()
    writer = MagicMock()
    writer.write.return_value = ReportV2(user_question="q", markdown_body="x")
    critic = MagicMock()
    critic.review.return_value = CriticVerdict(passed=False,
                                                  error_kind="off_topic")

    ctx = AblationContext(
        rewritten=RewrittenQuery(original="q",
                                  subtasks=[Subtask(text="q")]),
        execution=_exec_with_evidence(),
        planner=planner, writer=writer, critic=critic,
        nl2sql_memory=nl_memory, planner_memory=planner_memory,
    )
    runner = AblationRunner(context=ctx)
    verdict = CriticVerdict(passed=False, error_kind="off_topic",
                             causal_record_ids=["success::not_guilty"])
    assert runner(verdict, ["success::not_guilty"]) is False


def test_ablation_runner_handles_empty_suspect_list():
    ctx = AblationContext(
        rewritten=RewrittenQuery(original="q",
                                  subtasks=[Subtask(text="q")]),
        execution=PlanExecutionV2(),
        planner=MagicMock(), writer=MagicMock(), critic=MagicMock(),
    )
    assert AblationRunner(context=ctx)(
        CriticVerdict(passed=False), [],
    ) is False


def test_ablation_runner_replay_exception_fails_safe():
    planner = MagicMock()
    planner.plan_and_execute.side_effect = RuntimeError("boom")
    ctx = AblationContext(
        rewritten=RewrittenQuery(original="q",
                                  subtasks=[Subtask(text="q")]),
        execution=PlanExecutionV2(),
        planner=planner, writer=MagicMock(), critic=MagicMock(),
    )
    assert AblationRunner(context=ctx)(
        CriticVerdict(passed=False), ["x"],
    ) is False


def test_ablation_muted_recall_filters_suspect_id():
    nl_memory = MagicMock()
    nl_memory.recall_schema = lambda *a, **kw: [
        {"id": "schema::posts_v2::topic_id"},
        {"id": "success::guilty"},
    ]
    nl_memory.recall_success = lambda *a, **kw: []
    nl_memory.recall_errors = lambda *a, **kw: []

    from agents.ablation_runner import _muted_records
    saved = nl_memory.recall_schema
    with _muted_records(nl_memory, None, {"success::guilty"}):
        out = nl_memory.recall_schema()
    # Inside the context the suspect was filtered out
    assert all(r["id"] != "success::guilty" for r in out)
    # On exit the original method comes back
    assert nl_memory.recall_schema is saved


# ── Reflection API ───────────────────────────────────────────────────────────

def test_reflection_routes_register_in_app(monkeypatch):
    from api import app as app_mod
    routes = {r.path for r in app_mod.app.routes}
    assert "/reflection/chroma2" in routes
    assert "/reflection/chroma3" in routes
    assert "/reflection/log" in routes
    assert "/reflection/chroma2/{record_id}" in routes
    assert "/reflection/chroma3/{record_id}" in routes
