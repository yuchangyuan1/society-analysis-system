"""
Plan Verifier - rule-by-rule unit tests.

Each test pins one rule; together they cover the long tail of LLM routing
mistakes the verifier is meant to catch before the planner executes them.
"""
from __future__ import annotations

import pytest

from agents.plan_verifier import PlanVerifier
from models.query import (
    RewrittenQuery,
    Subtask,
    SubtaskTarget,
)


def _rq(text: str, intent: str = "freeform",
        branches=None, topic_id=None,
        metadata_filter=None) -> RewrittenQuery:
    return RewrittenQuery(
        original=text,
        subtasks=[Subtask(
            text=text, intent=intent,
            suggested_branches=list(branches or []),
            targets=SubtaskTarget(
                topic_id=topic_id,
                metadata_filter=dict(metadata_filter or {}),
            ),
        )],
    )


# ── R-OVERVIEW ───────────────────────────────────────────────────────────────

def test_overview_strips_kg_when_listing_topics():
    rq = _rq(
        "List the main topics in the selected Reddit data and summarize "
        "discussion volume and dominant emotion.",
        intent="freeform",
        branches=["nl2sql", "kg"],
    )
    plan = PlanVerifier().verify(rq)
    assert plan.was_modified
    sub = plan.rewritten.subtasks[0]
    assert sub.suggested_branches == ["nl2sql"]
    assert sub.intent == "community_listing"
    assert any(a.rule_id == "R-OVERVIEW" for a in plan.actions)
    assert any(s.branch == "kg" for s in plan.skipped_branches)


def test_overview_does_not_fire_when_user_pinned_topic():
    rq = _rq(
        "List posts in the Iran topic with high reply counts.",
        intent="community_listing", branches=["nl2sql", "kg"],
        topic_id="topic_iran123",
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    # Real targeted listing - keep KG; verifier should not strip it
    assert sub.suggested_branches == ["nl2sql", "kg"]


def test_overview_does_not_fire_when_propagation_words_present():
    rq = _rq(
        "Show all topics with the most propagation paths.",
        intent="community_listing", branches=["nl2sql", "kg"],
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert "kg" in sub.suggested_branches


# ── R-FACT-CHECK-EVIDENCE ────────────────────────────────────────────────────

def test_fact_check_must_have_evidence():
    rq = _rq(
        "Verify the claim that vaccines reduce hospitalisation by 90%.",
        intent="fact_check",
        branches=["nl2sql"],
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert "evidence" in sub.suggested_branches
    assert sub.suggested_branches.index("evidence") == 0
    assert any(a.rule_id == "R-FACT-CHECK-EVIDENCE" for a in plan.actions)


def test_fact_check_with_evidence_already_unchanged():
    rq = _rq(
        "Verify the claim about vaccines.",
        intent="fact_check",
        branches=["evidence", "nl2sql"],
    )
    plan = PlanVerifier().verify(rq)
    assert not any(a.rule_id == "R-FACT-CHECK-EVIDENCE" for a in plan.actions)


# ── R-INTENT-BRANCH-MATCH ────────────────────────────────────────────────────

@pytest.mark.parametrize("intent, text, topic_id", [
    # propagation_trace handled by R-PROPAGATION-ANCHORS - tested elsewhere.
    ("influencer_query",    "who amplifies the climate topic?",
     "topic_climate"),
    ("coordination_check",  "is this organised in topic about climate?",
     "topic_climate"),
    ("community_structure", "is the climate topic an echo chamber?",
     "topic_climate"),
    ("cascade_query",       "trace cascades in topic about climate.",
     "topic_climate"),
])
def test_kg_intents_get_kg_branch_added(intent, text, topic_id):
    rq = _rq(text, intent=intent, branches=["nl2sql"], topic_id=topic_id)
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert sub.suggested_branches[0] == "kg"
    assert any(a.rule_id == "R-INTENT-BRANCH-MATCH" for a in plan.actions)


def test_kg_intent_with_kg_present_unchanged():
    rq = _rq(
        "Trace propagation in the climate topic.",
        intent="cascade_query",
        branches=["kg"],
        topic_id="topic_climate",
    )
    plan = PlanVerifier().verify(rq)
    assert not any(a.rule_id == "R-INTENT-BRANCH-MATCH" for a in plan.actions)


# ── R-PROPAGATION-ANCHORS ────────────────────────────────────────────────────

def test_propagation_anchors_recovered_from_text():
    rq = _rq(
        "Trace the path from u_alice to u_bob.",
        intent="propagation_trace",
        branches=["kg"],
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert sub.targets.metadata_filter["source_account"] == "u_alice"
    assert sub.targets.metadata_filter["target_account"] == "u_bob"
    assert sub.intent == "propagation_trace"  # unchanged


def test_propagation_anchors_downgrade_to_cascade_when_topic_anchor():
    rq = _rq(
        "Trace propagation in topic about climate.",
        intent="propagation_trace",
        branches=["kg"],
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert sub.intent == "cascade_query"
    assert "kg" in sub.suggested_branches


def test_propagation_anchors_downgrade_to_freeform_when_no_topic():
    rq = _rq(
        "Trace propagation paths somewhere.",
        intent="propagation_trace",
        branches=["kg"],
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert sub.intent == "freeform"
    assert "kg" not in sub.suggested_branches
    assert any(s.branch == "kg" for s in plan.skipped_branches)


# ── R-KG-TOPIC-ANCHOR ────────────────────────────────────────────────────────

def test_cascade_query_without_topic_drops_kg():
    rq = _rq(
        "Show the longest reply cascade.",
        intent="cascade_query",
        branches=["kg"],
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert "kg" not in sub.suggested_branches
    assert any(a.rule_id == "R-KG-TOPIC-ANCHOR" for a in plan.actions)
    assert any(s.branch == "kg" and s.reason == "no_topic_anchor"
               for s in plan.skipped_branches)


def test_cascade_query_with_topic_id_keeps_kg():
    rq = _rq(
        "Show longest cascade.",
        intent="cascade_query",
        branches=["kg"],
        topic_id="topic_climate",
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert "kg" in sub.suggested_branches


def test_influencer_query_global_is_allowed():
    """influencer_query without topic anchor should NOT be dropped - the
    KG branch supports global PageRank when topic_id is None.
    """
    rq = _rq(
        "Who is most influential overall?",
        intent="influencer_query",
        branches=["kg", "nl2sql"],
    )
    plan = PlanVerifier().verify(rq)
    sub = plan.rewritten.subtasks[0]
    assert "kg" in sub.suggested_branches


# ── No-op pass case ──────────────────────────────────────────────────────────

def test_well_formed_plan_unchanged():
    rq = _rq(
        "Identify top amplifiers in the climate topic.",
        intent="influencer_query",
        branches=["kg", "nl2sql"],
        topic_id="topic_climate",
    )
    plan = PlanVerifier().verify(rq)
    assert not plan.was_modified
    assert plan.rewritten.subtasks[0].suggested_branches == ["kg", "nl2sql"]


# ── Multiple subtasks ────────────────────────────────────────────────────────

def test_rules_apply_per_subtask():
    rq = RewrittenQuery(
        original="x",
        subtasks=[
            Subtask(
                text="Verify the BBC story.",
                intent="fact_check",
                suggested_branches=["nl2sql"],
            ),
            Subtask(
                text="Trace propagation in topic about Iran.",
                intent="propagation_trace",
                suggested_branches=["kg"],
            ),
        ],
    )
    plan = PlanVerifier().verify(rq)
    rule_ids = [a.rule_id for a in plan.actions]
    assert "R-FACT-CHECK-EVIDENCE" in rule_ids
    assert "R-PROPAGATION-ANCHORS" in rule_ids
    assert plan.rewritten.subtasks[0].suggested_branches[0] == "evidence"
    assert plan.rewritten.subtasks[1].intent == "cascade_query"
