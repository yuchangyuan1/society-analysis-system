"""
Build the per-run InterventionDecision from the primary claim's actionability.

P0-2, §10.2 + §10.4. Lookup table only — no LLM, no side effects. The
intervention decision is scoped to the primary claim; all other claims
appear in the report as a short actionability list without triggering
their own visual pipeline.

Decision routing:
  actionable (contradicting >= 1)            → rebut (Rebuttal Card)
  non_actionable + supporting >= 2           → evidence_context (Context Card)
  non_actionable otherwise                   → abstain (text block only)
  primary_claim missing                      → abstain + no_primary_claim
"""
from __future__ import annotations

from typing import Optional

from models.claim import Claim
from models.report import InterventionDecision


_REASON_EXPLANATIONS = {
    "context_sparse": (
        "The claim lacks named entities or authoritative evidence in our "
        "retrieval corpus, so we cannot anchor a targeted rebuttal."
    ),
    "insufficient_evidence": (
        "The claim references identifiable entities but the classifier did "
        "not surface any directly contradicting evidence; issuing a rebuttal "
        "would overstate our confidence."
    ),
    "non_factual_expression": (
        "The claim is phrased as opinion or interpretation rather than a "
        "falsifiable factual statement; rebuttal is not the appropriate "
        "response mode."
    ),
}

_REASON_NEXT_STEPS = {
    "context_sparse": "monitor",
    "insufficient_evidence": "human_review",
    "non_factual_expression": "summarize",
}


def build_intervention_decision(
    primary_claim: Optional[Claim],
) -> InterventionDecision:
    if primary_claim is None:
        return InterventionDecision(
            decision="abstain",
            reason="no_primary_claim",
            explanation=(
                "No primary claim was selected for this run; no intervention "
                "target is defined."
            ),
            recommended_next_step="none",
            visual_type=None,
        )

    if primary_claim.claim_actionability == "actionable":
        return InterventionDecision(
            primary_claim_id=primary_claim.id,
            primary_claim_text=primary_claim.normalized_text,
            decision="rebut",
            reason=None,
            explanation=(
                f"Primary claim has {len(primary_claim.contradicting_evidence)} "
                f"contradicting evidence item(s); a targeted rebuttal card "
                f"will be generated."
            ),
            recommended_next_step="deploy",
            visual_type="rebuttal_card",
        )

    # non_actionable branch
    reason = primary_claim.non_actionable_reason
    supporting_count = len(primary_claim.supporting_evidence)

    if supporting_count >= 2:
        return InterventionDecision(
            primary_claim_id=primary_claim.id,
            primary_claim_text=primary_claim.normalized_text,
            decision="evidence_context",
            reason=reason,
            explanation=(
                "No directly contradicting evidence was found, but "
                f"{supporting_count} authoritative descriptions of the entities "
                "in the claim are available. The system will publish an "
                "Evidence/Context card that presents those facts alongside an "
                "explicit analyst note."
            ),
            recommended_next_step="publish_context",
            visual_type="evidence_context_card",
        )

    return InterventionDecision(
        primary_claim_id=primary_claim.id,
        primary_claim_text=primary_claim.normalized_text,
        decision="abstain",
        reason=reason,
        explanation=_REASON_EXPLANATIONS.get(
            reason or "",
            "The claim did not meet the threshold for a rebuttal or context card.",
        ),
        recommended_next_step=_REASON_NEXT_STEPS.get(reason or "", "monitor"),
        visual_type=None,
    )
