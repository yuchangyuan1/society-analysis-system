"""
Plan Verifier - redesign-2026-05-kg Phase 7 (post-screenshot hardening).

Inserted between QueryRewriter and BoundedPlannerV2. Validates and corrects
the rewriter's output BEFORE branches are executed, so the LLM's softer
hints don't reach the executor as un-checked routing decisions.

Each rule is a deterministic, side-effect-free check on a single Subtask.
Corrections are recorded as `VerifierAction` and forwarded into Chroma 3 by
the orchestrator, so the next Rewriter call can be primed with the same
anti-patterns as a negative few-shot.

Rules (run per subtask, in order):

  R-OVERVIEW              broad "list / main topics" question -> nl2sql only
  R-FACT-CHECK-EVIDENCE   fact_check intent must include the evidence branch
  R-INTENT-BRANCH-MATCH   KG-specialised intents must include the kg branch
  R-PROPAGATION-ANCHORS   propagation_trace needs two account ids; if missing,
                          recover from text or downgrade the intent
  R-KG-TOPIC-ANCHOR       cascade_query / community_structure need a topic
                          anchor; drop kg when neither is available

Why deterministic rules and not pure prompt engineering: the Rewriter's
JSON output is unstable (same prompt, same temperature, multiple plausible
outputs). Soft constraints in a 200-line system prompt hit diminishing
returns. Hard constraints applied AFTER the LLM call cost ~0.1 ms each and
are unit-testable; they catch the long tail that prompt rules miss.
"""
from __future__ import annotations

import re
from typing import Optional

import structlog

from models.query import (
    BranchName,
    RewrittenQuery,
    SkippedBranch,
    Subtask,
    SubtaskTarget,
    VerifiedPlan,
    VerifierAction,
)

log = structlog.get_logger(__name__)


# ── Intent classification ────────────────────────────────────────────────────

# Intents whose KG analysis is meaningful only WITH a topic anchor.
KG_REQUIRES_TOPIC_ANCHOR: set[str] = {
    "cascade_query",
    "community_structure",
}

# Intents whose KG analysis still produces useful global signal when no
# topic anchor is available (PageRank / Louvain over the full graph).
KG_ALLOWS_GLOBAL: set[str] = {
    "influencer_query",
    "coordination_check",
}

# Intents that require the kg branch to be present at all.
KG_REQUIRED_INTENTS: set[str] = (
    KG_REQUIRES_TOPIC_ANCHOR
    | KG_ALLOWS_GLOBAL
    | {"propagation_trace"}
)


# ── Pattern helpers ──────────────────────────────────────────────────────────

# Matches "list main topics" / "show all topics" / "what are today's topics" /
# "topics ... by post count" etc. The match is intentionally generous on the
# verb side and tight on the object side (the word "topic(s)" must appear).
_OVERVIEW_TEXT_RE = re.compile(
    r"\b(list|show|what(?:\s+are)?|which|all|main|top|today'?s|todays)\b"
    r"[^\n]*\btopics?\b"
    r"|\btopics?\b[^\n]*\b(topic_id|post\s*count|dominant\s*emotion|label|"
    r"count|volume|number|how\s+many)\b"
    r"|有哪些\s*topics?",
    re.IGNORECASE,
)

# If any of these verbs appear, the user wants graph-structural analysis,
# not a list of topics. Block R-OVERVIEW from misfiring.
_OVERVIEW_NEGATIVE_RE = re.compile(
    r"\b(amplif|influenc|propagat|spread|path|trace|cascade|central|"
    r"echo\s*chamber|coordinat|bot\s*network)\w*\b",
    re.IGNORECASE,
)

# "topic about X" / "the X topic" / "in topic T" / "for the topic Y" -
# evidence the user already pinned a single topic in natural language.
_SPECIFIC_TOPIC_RE = re.compile(
    r"\b(?:topic\s+about|the\s+\S+\s+topic|in\s+topic|inside\s+topic|"
    r"for\s+the\s+topic|within\s+the?\s*topic)\b",
    re.IGNORECASE,
)

# Confident account-id shapes only. Verifier stays pure (no PG round-trip);
# fuzzy plain-handle resolution is left to the runner.
_ACCOUNT_ID_RE = re.compile(
    r"\b(u_[a-zA-Z][a-zA-Z0-9_]*"
    r"|user_[a-zA-Z][a-zA-Z0-9_]*"
    r"|@[a-zA-Z][a-zA-Z0-9_]*)\b",
)


def _looks_like_overview(sub: Subtask) -> bool:
    text = (sub.text or "").lower()
    if not _OVERVIEW_TEXT_RE.search(text):
        return False
    if _OVERVIEW_NEGATIVE_RE.search(text):
        return False
    if sub.targets.topic_id:
        return False
    if _SPECIFIC_TOPIC_RE.search(text):
        return False
    return True


def _has_topic_anchor(sub: Subtask) -> bool:
    if sub.targets.topic_id:
        return True
    return bool(_SPECIFIC_TOPIC_RE.search(sub.text or ""))


def _extract_account_ids(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _ACCOUNT_ID_RE.finditer(text):
        tok = m.group(1)
        if tok.startswith("@"):
            tok = tok[1:]
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out[:5]


# ── PlanVerifier ─────────────────────────────────────────────────────────────

class PlanVerifier:
    """Validate / correct a RewrittenQuery before it enters the Planner."""

    def verify(self, rq: RewrittenQuery) -> VerifiedPlan:
        plan = VerifiedPlan(rewritten=rq.model_copy(deep=True))
        for idx, sub in enumerate(plan.rewritten.subtasks):
            self._apply_overview(plan, idx, sub)
            self._apply_fact_check_evidence(plan, idx, sub)
            self._apply_intent_branch_match(plan, idx, sub)
            self._apply_propagation_anchors(plan, idx, sub)
            self._apply_kg_topic_anchor(plan, idx, sub)
        if plan.was_modified:
            log.info(
                "plan_verifier.applied",
                rules=[a.rule_id for a in plan.actions],
                skipped=[
                    f"{s.branch}@{s.subtask_index}:{s.reason}"
                    for s in plan.skipped_branches
                ],
            )
        return plan

    # ── R-OVERVIEW ───────────────────────────────────────────────────────────

    def _apply_overview(
        self, plan: VerifiedPlan, idx: int, sub: Subtask,
    ) -> None:
        if not _looks_like_overview(sub):
            return
        before_branches = list(sub.suggested_branches)
        before_intent = sub.intent
        # Already minimal? skip.
        if before_branches == ["nl2sql"] and before_intent in {
            "community_listing", "community_count",
        }:
            return
        sub.suggested_branches = ["nl2sql"]
        if before_intent not in {
            "community_listing", "community_count", "trend",
        }:
            sub.intent = "community_listing"  # type: ignore[assignment]
        plan.actions.append(VerifierAction(
            rule_id="R-OVERVIEW",
            subtask_index=idx,
            detail="broad-topic listing query forced to nl2sql-only",
            before={"intent": before_intent, "branches": before_branches},
            after={"intent": sub.intent,
                   "branches": list(sub.suggested_branches)},
        ))
        for b in before_branches:
            if b != "nl2sql":
                plan.skipped_branches.append(SkippedBranch(
                    subtask_index=idx, branch=b,
                    reason="overview_query",
                    rule_id="R-OVERVIEW",
                ))

    # ── R-FACT-CHECK-EVIDENCE ────────────────────────────────────────────────

    def _apply_fact_check_evidence(
        self, plan: VerifiedPlan, idx: int, sub: Subtask,
    ) -> None:
        if sub.intent != "fact_check":
            return
        if "evidence" in sub.suggested_branches:
            return
        before = list(sub.suggested_branches)
        sub.suggested_branches = ["evidence"] + [
            b for b in before if b != "evidence"
        ]
        plan.actions.append(VerifierAction(
            rule_id="R-FACT-CHECK-EVIDENCE",
            subtask_index=idx,
            detail="fact_check requires the evidence branch",
            before={"branches": before},
            after={"branches": list(sub.suggested_branches)},
        ))

    # ── R-INTENT-BRANCH-MATCH ────────────────────────────────────────────────

    def _apply_intent_branch_match(
        self, plan: VerifiedPlan, idx: int, sub: Subtask,
    ) -> None:
        if sub.intent not in KG_REQUIRED_INTENTS:
            return
        if "kg" in sub.suggested_branches:
            return
        before = list(sub.suggested_branches)
        sub.suggested_branches = ["kg"] + [b for b in before if b != "kg"]
        plan.actions.append(VerifierAction(
            rule_id="R-INTENT-BRANCH-MATCH",
            subtask_index=idx,
            detail=f"intent={sub.intent} requires the kg branch",
            before={"branches": before},
            after={"branches": list(sub.suggested_branches)},
        ))

    # ── R-PROPAGATION-ANCHORS ────────────────────────────────────────────────

    def _apply_propagation_anchors(
        self, plan: VerifiedPlan, idx: int, sub: Subtask,
    ) -> None:
        if sub.intent != "propagation_trace":
            return
        meta = dict(sub.targets.metadata_filter or {})
        src = meta.get("source_account")
        dst = meta.get("target_account")
        if src and dst:
            return

        candidates = _extract_account_ids(sub.text or "")
        if not src and candidates:
            src = candidates[0]
        if not dst and len(candidates) >= 2:
            dst = candidates[1]
        if src and dst:
            new_meta = {**meta, "source_account": src,
                        "target_account": dst}
            sub.targets = sub.targets.model_copy(
                update={"metadata_filter": new_meta},
            )
            plan.actions.append(VerifierAction(
                rule_id="R-PROPAGATION-ANCHORS",
                subtask_index=idx,
                detail="filled source/target accounts from subtask text",
                before={"metadata_filter": meta},
                after={"metadata_filter": new_meta},
            ))
            return

        # Cannot recover anchors -> downgrade intent.
        before_intent = sub.intent
        before_branches = list(sub.suggested_branches)
        if _has_topic_anchor(sub):
            sub.intent = "cascade_query"  # type: ignore[assignment]
        else:
            sub.intent = "freeform"  # type: ignore[assignment]
            sub.suggested_branches = [
                b for b in sub.suggested_branches if b != "kg"
            ] or ["nl2sql"]
            if "kg" in before_branches and "kg" not in sub.suggested_branches:
                plan.skipped_branches.append(SkippedBranch(
                    subtask_index=idx, branch="kg",
                    reason="propagation_trace_no_anchors_no_topic",
                    rule_id="R-PROPAGATION-ANCHORS",
                ))
        plan.actions.append(VerifierAction(
            rule_id="R-PROPAGATION-ANCHORS",
            subtask_index=idx,
            detail="propagation_trace missing account anchors; downgraded intent",
            before={"intent": before_intent, "branches": before_branches},
            after={"intent": sub.intent,
                   "branches": list(sub.suggested_branches)},
        ))

    # ── R-KG-TOPIC-ANCHOR ────────────────────────────────────────────────────

    def _apply_kg_topic_anchor(
        self, plan: VerifiedPlan, idx: int, sub: Subtask,
    ) -> None:
        if sub.intent not in KG_REQUIRES_TOPIC_ANCHOR:
            return
        if _has_topic_anchor(sub):
            return
        before_branches = list(sub.suggested_branches)
        sub.suggested_branches = [
            b for b in before_branches if b != "kg"
        ] or ["nl2sql"]
        plan.actions.append(VerifierAction(
            rule_id="R-KG-TOPIC-ANCHOR",
            subtask_index=idx,
            detail=(
                f"intent={sub.intent} needs a topic anchor that is not "
                "available; dropped kg branch"
            ),
            before={"branches": before_branches},
            after={"branches": list(sub.suggested_branches)},
        ))
        if "kg" in before_branches:
            plan.skipped_branches.append(SkippedBranch(
                subtask_index=idx, branch="kg",
                reason="no_topic_anchor",
                rule_id="R-KG-TOPIC-ANCHOR",
            ))
