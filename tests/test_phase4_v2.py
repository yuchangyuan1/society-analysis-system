"""
Phase 4 (redesign-2026-05) unit tests.

Coverage:
- QueryRewriter: subtask split, context inheritance, fallback degrade
- BoundedPlannerV2: branch routing, parallel exec, bounded caps, runner errors
- ReportWriter: payload composition, fallback when LLM fails
- QualityCritic: citation completeness, numeric consistency, LLM-skip path
- ChatOrchestrator: end-to-end happy path + Critic-retry path

LLM / DB / Chroma calls are mocked everywhere.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.planner_v2 import (
    BoundedPlannerV2,
    BranchInvocation,
    PlanExecutionV2,
)
from agents.query_rewriter import QueryRewriter
from agents.quality_critic import QualityCritic
from agents.report_writer import ReportWriter
from models.branch_output import (
    BranchExecutionStatus,
    EvidenceOutput,
    KGOutput,
    SQLOutput,
)
from models.evidence import Citation, EvidenceBundle, EvidenceChunk
from models.query import RewrittenQuery, Subtask, SubtaskTarget
from models.reflection import CriticVerdict
from models.report_v2 import ReportNumber, ReportV2
from models.session import SessionState


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


# ── QueryRewriter ────────────────────────────────────────────────────────────

def test_rewriter_splits_into_subtasks():
    client = _mock_openai(json.dumps({
        "subtasks": [
            {"text": "What did BBC say?",
             "intent": "official_recap",
             "suggested_branches": ["evidence"],
             "targets": {"topic_id": "t1"},
             "rationale": "fact-side"},
            {"text": "How many posts in topic t1?",
             "intent": "community_count",
             "suggested_branches": ["nl2sql"],
             "targets": {"topic_id": "t1"},
             "rationale": "volume-side"},
        ]
    }))
    rw = QueryRewriter(client=client)
    rq = rw.rewrite("Compare official line vs Reddit volume on topic t1",
                    session=None)
    assert len(rq.subtasks) == 2
    assert rq.subtasks[0].intent == "official_recap"
    assert rq.subtasks[1].suggested_branches == ["nl2sql"]
    assert rq.is_multistep


def test_rewriter_inherits_session_targets_when_llm_omits():
    state = SessionState(session_id="s1",
                          current_topic_id="t9", current_run_id="run-x")
    client = _mock_openai(json.dumps({
        "subtasks": [
            {"text": "What's the dominant emotion here?",
             "intent": "community_count",
             "suggested_branches": ["nl2sql"],
             "targets": {},
             "rationale": ""},
        ]
    }))
    rq = QueryRewriter(client=client).rewrite("emotion in this topic", state)
    sub = rq.subtasks[0]
    assert sub.targets.topic_id == "t9"
    assert sub.targets.run_id == "run-x"


def test_rewriter_degrades_on_llm_error():
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("api down")
    rq = QueryRewriter(client=client).rewrite("hello", None)
    assert len(rq.subtasks) == 1
    assert rq.fallback_reason and rq.fallback_reason.startswith("llm_error")


def test_rewriter_drops_invalid_subtasks():
    client = _mock_openai(json.dumps({
        "subtasks": [
            {"text": "", "intent": "freeform"},          # blank text
            {"text": "valid", "intent": "weird_intent"}, # bad intent collapses to freeform
        ]
    }))
    rq = QueryRewriter(client=client).rewrite("anything", None)
    assert len(rq.subtasks) == 1
    assert rq.subtasks[0].intent == "freeform"


def test_rewriter_filters_unknown_branches():
    client = _mock_openai(json.dumps({
        "subtasks": [
            {"text": "x",
             "intent": "fact_check",
             "suggested_branches": ["evidence", "weird"],
             "targets": {},
             "rationale": ""},
        ]
    }))
    rq = QueryRewriter(client=client).rewrite("anything", None)
    assert rq.subtasks[0].suggested_branches == ["evidence"]


# ── BoundedPlannerV2 ─────────────────────────────────────────────────────────

def _evidence_runner(inv):
    return EvidenceOutput(bundle=EvidenceBundle(query=inv.payload.get("query", ""),
                                                  chunks=[EvidenceChunk(
                                                      chunk_id="c1", text="t",
                                                      citation=Citation(
                                                          chunk_id="c1",
                                                          source="bbc",
                                                          domain="bbc.com",
                                                      ),
                                                      rrf_score=0.1)]))


def _nl2sql_runner(inv):
    return SQLOutput(nl_query=inv.payload.get("nl_query", ""),
                     final_sql="SELECT 1", rows=[{"x": 1}], success=True)


def _kg_runner(inv):
    return KGOutput(query_kind="key_nodes", target={},
                     metrics={"posts": 3})


def test_planner_routes_intent_to_branches():
    # fact_check fans out to evidence + nl2sql (multi-branch by default).
    rq = RewrittenQuery(
        original="x",
        subtasks=[Subtask(text="t", intent="fact_check")],
    )
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
    )
    execution = planner.plan_and_execute(rq)
    assert set(execution.branches_used) == {"evidence", "nl2sql"}
    assert all(r.status.success for r in execution.results)


def test_planner_runs_multiple_branches_in_parallel():
    rq = RewrittenQuery(
        original="x",
        subtasks=[
            Subtask(text="A", intent="official_recap"),
            Subtask(text="B", intent="community_count"),
            Subtask(text="C", intent="propagation"),
        ],
    )
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
    )
    execution = planner.plan_and_execute(rq)
    assert set(execution.branches_used) == {"evidence", "nl2sql", "kg"}


def test_planner_caps_branch_calls():
    # 3 subtasks each with 2 branches -> 6 invocations but cap=4
    rq = RewrittenQuery(
        original="x",
        subtasks=[
            Subtask(text=f"q{i}", intent="comparison",
                    suggested_branches=["evidence", "nl2sql"])
            for i in range(3)
        ],
    )
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
        max_branch_calls=4,
    )
    execution = planner.plan_and_execute(rq)
    assert len(execution.workflow) == 4


def test_planner_handles_runner_exception():
    def boom(inv):
        raise RuntimeError("boom")

    # Use suggested_branches=['evidence'] so only the boom-runner is invoked.
    rq = RewrittenQuery(
        original="x",
        subtasks=[Subtask(text="t", intent="fact_check",
                          suggested_branches=["evidence"])],
    )
    planner = BoundedPlannerV2(
        evidence_runner=boom,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
    )
    execution = planner.plan_and_execute(rq)
    assert all(not r.status.success for r in execution.results)
    assert execution.results[0].status.error_kind == "branch_runner_error"


def test_planner_few_shot_skipped_when_high_confidence():
    pm = MagicMock()
    pm.count_branch_combo_successes.return_value = 9
    pm.recall_workflow_exemplars = MagicMock()

    rq = RewrittenQuery(original="x",
                         subtasks=[Subtask(text="t", intent="fact_check")])
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
        planner_memory=pm,
        embeddings=MagicMock(),
    )
    execution = planner.plan_and_execute(rq)
    assert execution.used_few_shot is False
    pm.recall_workflow_exemplars.assert_not_called()


def test_planner_few_shot_recalled_when_low_confidence():
    pm = MagicMock()
    pm.count_branch_combo_successes.return_value = 1
    pm.recall_workflow_exemplars.return_value = []
    embeddings = MagicMock()
    embeddings.embed.return_value = [0.0] * 4

    rq = RewrittenQuery(original="x",
                         subtasks=[Subtask(text="t", intent="fact_check")])
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
        planner_memory=pm,
        embeddings=embeddings,
    )
    execution = planner.plan_and_execute(rq)
    assert execution.used_few_shot is True
    pm.recall_workflow_exemplars.assert_called_once()


# ── ReportWriter ─────────────────────────────────────────────────────────────

def _exec_with(branches):
    workflow = []
    results = []
    for i, (branch, output) in enumerate(branches):
        inv = BranchInvocation(subtask_index=0, branch=branch, payload={})
        workflow.append(inv)
        from agents.planner_v2 import BranchResult
        results.append(BranchResult(
            invocation=inv,
            status=BranchExecutionStatus(branch=branch, success=True),
            output=output.model_dump(),
        ))
    branch_names = [b for b, _ in branches]
    return PlanExecutionV2(workflow=workflow, results=results,
                            branches_used=branch_names)


def test_report_writer_aggregates_citations_even_without_llm():
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("offline")
    writer = ReportWriter(client=client)

    bundle = EvidenceBundle(query="q", chunks=[
        EvidenceChunk(chunk_id="c1", text="x", citation=Citation(
            chunk_id="c1", source="bbc", domain="bbc.com")),
    ])
    rq = RewrittenQuery(original="q", subtasks=[Subtask(text="q")])
    execution = _exec_with([("evidence", EvidenceOutput(bundle=bundle))])
    report = writer.write(rq, execution)
    assert any(c.chunk_id == "c1" for c in report.citations)
    # Body falls back to a deterministic summary
    assert "evidence chunks" in report.markdown_body


def test_report_writer_no_branch_output_returns_explainer():
    rq = RewrittenQuery(original="q", subtasks=[Subtask(text="q")])
    execution = PlanExecutionV2()
    writer = ReportWriter(client=MagicMock())
    report = writer.write(rq, execution)
    assert "no_branch_output" in report.notes
    assert "couldn't gather" in report.markdown_body.lower()


def test_report_writer_uses_llm_response_with_numbers():
    client = _mock_openai(json.dumps({
        "markdown_body": "There are 3 posts about it.",
        "numbers": [{"label": "post_count", "value": 3,
                      "source_branch": "nl2sql",
                      "source_ref": "SELECT count(*) FROM posts_v2"}],
    }))
    writer = ReportWriter(client=client)
    sql = SQLOutput(nl_query="how many?", final_sql="SELECT 1",
                     rows=[{"x": 1}, {"x": 2}, {"x": 3}], success=True)
    rq = RewrittenQuery(original="q", subtasks=[Subtask(text="q")])
    execution = _exec_with([("nl2sql", sql)])
    report = writer.write(rq, execution)
    assert "3 posts" in report.markdown_body
    assert report.numbers and report.numbers[0].label == "post_count"


# ── QualityCritic ────────────────────────────────────────────────────────────

def test_critic_passes_when_no_problems(monkeypatch):
    critic = QualityCritic(client=_mock_openai(json.dumps({
        "on_topic": True, "hallucination": False,
        "reason_on_topic": "", "reason_hallucination": ""})))
    report = ReportV2(
        user_question="q",
        markdown_body="Looking at [c1] official sources...",
        citations=[Citation(chunk_id="c1", source="bbc", domain="bbc.com")],
        branches_used=["evidence"],
    )
    bundle = EvidenceBundle(query="q", chunks=[EvidenceChunk(
        chunk_id="c1", text="x",
        citation=Citation(chunk_id="c1", source="bbc", domain="bbc.com"))])
    execution = _exec_with([("evidence", EvidenceOutput(bundle=bundle))])
    verdict = critic.review(report, execution)
    assert verdict.passed


def test_critic_flags_unknown_citation_token():
    critic = QualityCritic(client=MagicMock())
    report = ReportV2(
        user_question="q",
        markdown_body="See [c_unknown] for details.",
        citations=[Citation(chunk_id="c1", source="bbc", domain="bbc.com")],
    )
    bundle = EvidenceBundle(query="q", chunks=[EvidenceChunk(
        chunk_id="c1", text="x",
        citation=Citation(chunk_id="c1", source="bbc", domain="bbc.com"))])
    execution = _exec_with([("evidence", EvidenceOutput(bundle=bundle))])
    verdict = critic.review(report, execution)
    assert verdict.passed is False
    assert verdict.error_kind == "citation_missing"


def test_critic_flags_numeric_mismatch_against_sql():
    critic = QualityCritic(client=MagicMock())
    report = ReportV2(
        user_question="q",
        markdown_body="There are 99 posts.",
        numbers=[ReportNumber(label="post_count", value=99,
                                source_branch="nl2sql")],
    )
    sql = SQLOutput(nl_query="?", final_sql="SELECT 1",
                     rows=[{"x": 1}, {"x": 2}], success=True)
    execution = _exec_with([("nl2sql", sql)])
    verdict = critic.review(report, execution)
    assert verdict.passed is False
    assert verdict.error_kind == "numeric_mismatch"


def test_critic_accepts_row_count_as_value():
    critic = QualityCritic(client=_mock_openai(json.dumps({
        "on_topic": True, "hallucination": False,
        "reason_on_topic": "", "reason_hallucination": ""})))
    report = ReportV2(
        user_question="q",
        markdown_body="2 posts.",
        numbers=[ReportNumber(label="post_count", value=2,
                                source_branch="nl2sql")],
    )
    sql = SQLOutput(nl_query="?", final_sql="SELECT 1",
                     rows=[{"x": 1}, {"x": 2}], success=True)
    execution = _exec_with([("nl2sql", sql)])
    verdict = critic.review(report, execution)
    assert verdict.passed


def test_critic_skips_when_llm_unavailable():
    """Programmatic checks pass; LLM raises -> critic returns lenient pass."""
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("offline")
    critic = QualityCritic(client=client)
    report = ReportV2(user_question="q", markdown_body="Some text [c1]",
                       citations=[Citation(chunk_id="c1", source="bbc",
                                            domain="bbc.com")])
    bundle = EvidenceBundle(query="q", chunks=[EvidenceChunk(
        chunk_id="c1", text="x",
        citation=Citation(chunk_id="c1", source="bbc", domain="bbc.com"))])
    execution = _exec_with([("evidence", EvidenceOutput(bundle=bundle))])
    verdict = critic.review(report, execution)
    assert verdict.passed
    assert verdict.notes.startswith("llm_skip")


def test_critic_flags_off_topic_via_llm():
    client = _mock_openai(json.dumps({
        "on_topic": False, "hallucination": False,
        "reason_on_topic": "answers a different question",
        "reason_hallucination": ""}))
    critic = QualityCritic(client=client)
    report = ReportV2(user_question="q", markdown_body="Some text")
    execution = PlanExecutionV2()
    verdict = critic.review(report, execution)
    assert verdict.error_kind == "off_topic"
    assert verdict.failed_branch == "writer"


# ── ChatOrchestrator ─────────────────────────────────────────────────────────

def test_orchestrator_happy_path(tmp_path, monkeypatch):
    """End-to-end smoke: rewriter -> planner -> writer -> critic -> response."""
    import config as cfg
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    # session_store reads from cfg.DATA_DIR at import-time; reset
    import services.session_store as ss
    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path / "sessions")

    rewriter = MagicMock()
    rewriter.rewrite.return_value = RewrittenQuery(
        original="q",
        subtasks=[Subtask(text="t", intent="fact_check",
                          suggested_branches=["evidence"])],
    )
    planner = MagicMock()
    bundle = EvidenceBundle(query="q", chunks=[EvidenceChunk(
        chunk_id="c1", text="x",
        citation=Citation(chunk_id="c1", source="bbc", domain="bbc.com"))])
    plan = _exec_with([("evidence", EvidenceOutput(bundle=bundle))])
    planner.plan_and_execute.return_value = plan

    writer = MagicMock()
    writer.write.return_value = ReportV2(
        user_question="q",
        markdown_body="Per [c1], yes.",
        citations=[bundle.chunks[0].citation],
    )

    critic = MagicMock()
    critic.review.return_value = CriticVerdict(passed=True)

    reflection = MagicMock()

    from agents.chat_orchestrator import ChatOrchestrator
    orch = ChatOrchestrator(
        rewriter=rewriter, planner=planner, writer=writer,
        critic=critic, reflection=reflection,
    )
    resp = orch.handle(session_id="s1", message="hello?")
    assert resp.session_id == "s1"
    assert resp.answer_text == "Per [c1], yes."
    assert resp.branches_used == ["evidence"]
    assert "evidence" in resp.branch_outputs
    assert resp.needs_human_review is False
    reflection.record.assert_called_once()


def test_orchestrator_critic_retry_then_human_review(tmp_path, monkeypatch):
    import services.session_store as ss
    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path / "sessions")

    rewriter = MagicMock()
    rewriter.rewrite.return_value = RewrittenQuery(
        original="q", subtasks=[Subtask(text="t", intent="fact_check")])
    planner = MagicMock()
    planner.plan_and_execute.return_value = _exec_with([])

    writer = MagicMock()
    writer.write.return_value = ReportV2(
        user_question="q", markdown_body="bad answer")

    critic = MagicMock()
    # Both attempts fail
    critic.review.return_value = CriticVerdict(
        passed=False, error_kind="off_topic", failed_branch="writer",
    )
    reflection = MagicMock()

    from agents.chat_orchestrator import ChatOrchestrator
    orch = ChatOrchestrator(
        rewriter=rewriter, planner=planner, writer=writer,
        critic=critic, reflection=reflection,
    )
    resp = orch.handle("s2", "hello?")
    assert writer.write.call_count == 2  # original + 1 retry
    assert resp.needs_human_review is True
