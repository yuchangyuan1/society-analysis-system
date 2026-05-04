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
    _first_kg_output_with_signal,
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
from tools.topic_resolver import _exact_label_matches


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


def test_rewriter_topic_reply_chains_use_cascade_query():
    client = _mock_openai(json.dumps({
        "subtasks": [
            {"text": "Show reply chains for the topic about Trump foreign policy criticism.",
             "intent": "propagation_trace",
             "suggested_branches": ["kg"],
             "targets": {},
             "rationale": "reply chain"},
        ]
    }))
    rq = QueryRewriter(client=client).rewrite(
        "Show reply chains for the topic about Trump foreign policy criticism.",
        None,
    )
    assert rq.subtasks[0].intent == "cascade_query"
    assert rq.subtasks[0].suggested_branches == ["kg"]


def test_rewriter_topic_propagation_path_uses_cascade_query():
    client = _mock_openai(json.dumps({
        "subtasks": [
            {"text": "Trace the propagation path of the US Military Arms Sales Controversy.",
             "intent": "propagation_trace",
             "suggested_branches": ["kg"],
             "targets": {},
             "rationale": "propagation path"},
        ]
    }))
    rq = QueryRewriter(client=client).rewrite(
        "Trace the propagation path of the US Military Arms Sales Controversy.",
        None,
    )
    assert rq.subtasks[0].intent == "cascade_query"
    assert rq.subtasks[0].suggested_branches == ["kg"]


def test_rewriter_topic_claim_audit_routes_to_sql_and_evidence():
    client = _mock_openai(json.dumps({
        "subtasks": [
            {"text": "For the Mexico City sinking topic, identify claims that are consistent with official sources, contradicted, or insufficient evidence.",
             "intent": "freeform",
             "suggested_branches": ["evidence", "nl2sql", "kg"],
             "targets": {"topic_id": "Mexico City sinking"},
             "rationale": "claim audit"},
        ]
    }))
    rq = QueryRewriter(client=client).rewrite(
        "For the Mexico City sinking topic, identify claims that are consistent with official sources, contradicted, or insufficient evidence.",
        None,
    )
    assert rq.subtasks[0].intent == "topic_claim_audit"
    assert rq.subtasks[0].suggested_branches == ["nl2sql", "evidence"]


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


def test_planner_resolves_label_topic_target_before_kg():
    class Match:
        topic_id = "topic_real123"
        label = "Real Topic"
        similarity = 0.9

    resolver = MagicMock()
    resolver.resolve.return_value = [Match()]
    resolver.resolve_candidates.return_value = [Match()]
    rq = RewrittenQuery(
        original="x",
        subtasks=[Subtask(
            text="For Global Politics, rank amplifiers",
            intent="influencer_query",
            suggested_branches=["kg"],
            targets={"topic_id": "Global Politics"},
        )],
    )
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
        topic_resolver=resolver,
    )
    plan = planner._plan(rq)
    assert plan[0].payload["topic_id"] == "topic_real123"


def test_planner_keeps_query_text_for_kg_cascade():
    rq = RewrittenQuery(
        original="Show reply chains for the topic about Trump foreign policy criticism.",
        subtasks=[Subtask(
            text="Show reply chains for the topic about Trump foreign policy criticism.",
            intent="cascade_query",
            suggested_branches=["kg"],
            targets=SubtaskTarget(topic_id="topic_abc123"),
        )],
    )
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
    )
    plan = planner._plan(rq)
    assert plan[0].payload["intent"] == "cascade_query"
    assert "reply chains" in plan[0].payload["query"].lower()


def test_planner_resolves_topic_for_propagation_trace_fallback():
    class Match:
        topic_id = "topic_real123"
        label = "Real Topic"
        similarity = 0.9

    resolver = MagicMock()
    resolver.resolve.return_value = [Match()]
    resolver.resolve_candidates.return_value = [Match()]
    rq = RewrittenQuery(
        original="Trace the propagation path of US Military Arms Sales Controversy.",
        subtasks=[Subtask(
            text="Trace the propagation path of US Military Arms Sales Controversy.",
            intent="propagation_trace",
            suggested_branches=["kg"],
        )],
    )
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
        topic_resolver=resolver,
    )
    plan = planner._plan(rq)
    assert plan[0].payload["topic_id"] == "topic_real123"


def test_planner_kg_candidate_fallback_uses_first_graph_signal():
    calls: list[str] = []

    def factory(topic_id: str) -> KGOutput:
        calls.append(topic_id)
        if topic_id == "topic_empty":
            return KGOutput(
                query_kind="reply_chains",
                target={"topic_id": topic_id},
                metrics={"node_count": 0, "edge_count": 0},
            )
        return KGOutput(
            query_kind="reply_chains",
            target={"topic_id": topic_id},
            nodes=[{"id": "p1", "label": "Post", "properties": {}}],
        )

    out = _first_kg_output_with_signal(
        ["topic_empty", "topic_with_graph"],
        factory,
    )
    assert calls == ["topic_empty", "topic_with_graph"]
    assert out.target["topic_id"] == "topic_with_graph"


def test_topic_resolver_exact_label_match_from_long_question():
    rows = [
        {"topic_id": "topic_a", "label": "US Military Sales and Funding Critique",
         "post_count": 86},
        {"topic_id": "topic_b", "label": "US Military Arms Sales Controversy",
         "post_count": 136},
    ]
    matches = _exact_label_matches(
        "Trace the propagation path of the US Military Arms Sales Controversy.",
        rows,
    )
    assert matches[0].topic_id == "topic_b"


def test_planner_broad_topic_listing_uses_only_nl2sql_without_topic_hints():
    resolver = MagicMock()
    rq = RewrittenQuery(
        original="List the topics from today's worldnews data with topic_id",
        subtasks=[Subtask(
            text="List the topics from today's worldnews data with topic_id, "
                 "label, post count, and dominant emotion.",
            intent="community_listing",
            suggested_branches=["nl2sql", "kg"],
            targets=SubtaskTarget(
                topic_id="Misleading Media Headlines Critique",
            ),
        )],
    )
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
        topic_resolver=resolver,
    )
    plan = planner._plan(rq)
    assert [inv.branch for inv in plan] == ["nl2sql"]
    assert "topic_id_hints" not in plan[0].payload
    resolver.resolve.assert_not_called()


def test_planner_topic_claim_audit_resolves_topic_and_keeps_sql_first():
    class Match:
        topic_id = "topic_mexico123"
        label = "Mexico City Sinking"
        similarity = 0.91

    resolver = MagicMock()
    resolver.resolve.return_value = [Match()]
    resolver.resolve_candidates.return_value = [Match()]
    rq = RewrittenQuery(
        original="audit claims",
        subtasks=[Subtask(
            text="For the Mexico City sinking topic, classify claims against official evidence.",
            intent="topic_claim_audit",
            suggested_branches=["nl2sql", "evidence"],
            targets=SubtaskTarget(topic_id="Mexico City sinking"),
        )],
    )
    planner = BoundedPlannerV2(
        evidence_runner=_evidence_runner,
        nl2sql_runner=_nl2sql_runner,
        kg_runner=_kg_runner,
        topic_resolver=resolver,
    )
    plan = planner._plan(rq)
    assert [inv.branch for inv in plan] == ["nl2sql", "evidence"]
    assert plan[0].payload["intent"] == "topic_claim_audit"
    assert plan[0].payload["topic_id_hints"][0]["topic_id"] == "topic_mexico123"
    assert "Mexico City Sinking" in plan[1].payload["query"]


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


def test_report_writer_refuses_topic_counts_when_nl2sql_failed():
    from agents.planner_v2 import BranchResult

    client = MagicMock()
    inv = BranchInvocation(subtask_index=0, branch="nl2sql", payload={})
    execution = PlanExecutionV2(
        workflow=[inv],
        results=[BranchResult(
            invocation=inv,
            status=BranchExecutionStatus(
                branch="nl2sql",
                success=False,
                error="Error finding id",
                error_kind="branch_runner_error",
            ),
        )],
        branches_used=["nl2sql"],
    )
    rq = RewrittenQuery(
        original="List topics with post counts",
        subtasks=[Subtask(text="List topics", intent="community_count")],
    )
    report = ReportWriter(client=client).write(rq, execution)
    assert report.needs_human_review
    assert "nl2sql" in report.notes[0]
    client.chat.completions.create.assert_not_called()


def test_report_writer_fact_check_payload_marks_empty_sources():
    rq = RewrittenQuery(
        original="Verify this claim against official sources and Reddit.",
        subtasks=[Subtask(text="Verify claim", intent="fact_check")],
    )
    bundle = EvidenceBundle(query="claim", chunks=[])
    sql = SQLOutput(
        nl_query="verify claim",
        final_sql="SELECT * FROM posts_v2 WHERE title ILIKE '%claim%'",
        rows=[],
        success=True,
    )
    execution = _exec_with([
        ("evidence", EvidenceOutput(bundle=bundle)),
        ("nl2sql", sql),
    ])
    payload = ReportWriter._build_payload(
        rq,
        execution,
        [EvidenceOutput(bundle=bundle)],
        [sql],
        [],
        [],
    )
    assert "Fact-check mode" in payload
    assert "Official evidence availability: 0 retrieved chunk(s)" in payload
    assert "rows (0 total): []" in payload
    assert "no matching rows were retrieved" in payload


def test_report_writer_fact_check_appends_empty_reddit_guard():
    client = _mock_openai(json.dumps({
        "markdown_body": (
            "Verdict: Insufficient evidence.\n\n"
            "Reddit discussion includes several matching posts."
        ),
        "numbers": [],
    }))
    rq = RewrittenQuery(
        original="Fact-check this claim using official sources and Reddit.",
        subtasks=[Subtask(text="Fact-check claim", intent="fact_check")],
    )
    sql = SQLOutput(
        nl_query="claim",
        final_sql="SELECT * FROM posts_v2 WHERE title ILIKE '%claim%'",
        rows=[],
        success=True,
    )
    execution = _exec_with([("nl2sql", sql)])
    report = ReportWriter(client=client).write(rq, execution)
    assert "fact_check_reddit_rows_empty" in report.notes
    assert "SQL returned 0 matching rows" in report.markdown_body


def test_report_writer_topic_claim_audit_payload_includes_rows_and_rules():
    rq = RewrittenQuery(
        original="For this topic, which claims are consistent, contradicted, or insufficient evidence?",
        subtasks=[Subtask(
            text="Audit topic claims",
            intent="topic_claim_audit",
        )],
    )
    bundle = EvidenceBundle(query="topic", chunks=[EvidenceChunk(
        chunk_id="ev1",
        text="Official source says Mexico City is sinking quickly.",
        citation=Citation(chunk_id="ev1", source="ap", domain="apnews.com"),
    )])
    sql = SQLOutput(
        nl_query="topic rows",
        final_sql="SELECT post_id, author, text FROM posts_v2",
        rows=[{
            "post_id": "reddit_1",
            "author": "alice",
            "text": "Mexico City is sinking so quickly it can be seen from space",
        }],
        success=True,
    )
    execution = _exec_with([
        ("nl2sql", sql),
        ("evidence", EvidenceOutput(bundle=bundle)),
    ])
    payload = ReportWriter._build_payload(
        rq,
        execution,
        [EvidenceOutput(bundle=bundle)],
        [sql],
        [],
        [],
    )
    assert "Topic claim audit mode" in payload
    assert "author" in payload
    assert "reddit_1" in payload
    assert "ev1" in payload


def test_report_writer_expands_raw_chunk_id_to_readable_source():
    client = _mock_openai(json.dumps({
        "markdown_body": "Evidence supports it [chunk_id: ev123456789abc].",
        "numbers": [],
    }))
    writer = ReportWriter(client=client)
    bundle = EvidenceBundle(query="q", chunks=[EvidenceChunk(
        chunk_id="ev123456789abc",
        text="Mexico City is sinking so quickly, it can be seen from space.",
        citation=Citation(
            chunk_id="ev123456789abc",
            source="ap",
            domain="apnews.com",
            title="Mexico City is sinking so quickly, it can be seen from space",
            url="https://apnews.com/article/example",
        ),
    )])
    rq = RewrittenQuery(original="q", subtasks=[Subtask(text="q")])
    execution = _exec_with([("evidence", EvidenceOutput(bundle=bundle))])
    report = writer.write(rq, execution)
    assert "ev123456789abc" not in report.markdown_body
    assert "AP" in report.markdown_body.upper()
    assert "https://apnews.com/article/example" in report.markdown_body


# ── QualityCritic ────────────────────────────────────────────────────────────

def test_report_writer_preserves_topic_id_when_requested():
    client = _mock_openai(json.dumps({
        "markdown_body": "Topic ID: topic_abcdef123456, Label: Example",
        "numbers": [],
    }))
    writer = ReportWriter(client=client)
    sql = SQLOutput(
        nl_query="list topics",
        final_sql="SELECT topic_id, label FROM topics_v2",
        rows=[{"topic_id": "topic_abcdef123456", "label": "Example"}],
        success=True,
    )
    rq = RewrittenQuery(
        original="List topics with topic_id and label",
        subtasks=[Subtask(text="List topics with topic_id and label")],
    )
    execution = _exec_with([("nl2sql", sql)])
    report = writer.write(rq, execution)
    assert "Topic ID: topic_abcdef123456" in report.markdown_body


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


def test_critic_allows_markdown_source_links_without_chunk_ids():
    critic = QualityCritic(client=MagicMock())
    report = ReportV2(
        user_question="q",
        markdown_body="Evidence: [AP News](https://apnews.com/example).",
        citations=[Citation(chunk_id="c1", source="ap", domain="apnews.com")],
    )
    verdict = critic._check_citations(report)
    assert verdict is None


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
