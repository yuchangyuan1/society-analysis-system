"""VisualSummaryCapability — 'summarize this in one image'.

Implements the chat-time equivalent of planner.py Step 9 (line 721-776):

  intervention_decision.decision == "rebut"
      → counter_message must exist
      → render clarification card
  intervention_decision.decision == "evidence_context"
      → claim has ≥2 supporting_evidence
      → render evidence-context card
  intervention_decision.decision == "abstain" / None
      → no PNG; return structured abstention block explaining why

The capability NEVER invents a decision — it only reads what the precompute
pipeline already stored in report_raw.json. If the run has no
intervention_decision, it returns `status="no_decision"`.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from capabilities.base import (
    Capability, CapabilityInput, CapabilityOutput, register_capability,
)
from tools.run_query_tools import (
    get_claim_details, GetClaimDetailsInput,
)
from tools.decision_tools import (
    get_intervention_decision, GetInterventionDecisionInput,
    InterventionDecisionView,
)
from tools.visual_tools import (
    generate_clarification_card, GenerateClarificationCardInput,
    generate_evidence_context_card, GenerateEvidenceContextCardInput,
    ClaimPayload,
)


VisualStatus = Literal[
    "rendered",          # PNG produced
    "abstained",         # decision.abstain → no PNG by design
    "no_decision",       # run has no intervention_decision
    "render_failed",     # visual call returned path=None
    "insufficient_data", # evidence_context with < 2 supporting items
]


class VisualSummaryInput(CapabilityInput):
    run_id: str = "latest"
    claim_id: Optional[str] = None  # defaults to primary claim


class VisualSummaryOutput(CapabilityOutput):
    run_id: str
    source: str
    status: VisualStatus
    image_path: Optional[str] = None
    decision: Optional[InterventionDecisionView] = None
    claim_id: Optional[str] = None
    claim_text: Optional[str] = None
    explanation: str = ""  # plain-text description for AnswerComposer
    reason: Optional[str] = None


class VisualSummaryCapability(Capability):
    name = "visual_summary"
    description = (
        "Render a visual summary card for a topic or claim. Decision precedes "
        "visual: rebut → Clarification Card PNG; clarify → Evidence Context "
        "Card PNG; abstain → structured abstention_block (no PNG). Answers "
        "'show me a summary card', 'render this claim', 'visualize topic X'."
    )
    example_utterances = [
        "用一张图总结这个话题",
        "render a summary card",
        "给我看一张图",
        "可视化一下",
    ]
    tags = ["visual", "image", "card", "sd"]
    Input = VisualSummaryInput
    Output = VisualSummaryOutput

    def run(self, input: VisualSummaryInput) -> VisualSummaryOutput:
        dec_out = get_intervention_decision(
            GetInterventionDecisionInput(run_id=input.run_id)
        )
        decision = dec_out.decision

        if decision is None:
            return VisualSummaryOutput(
                run_id=dec_out.run_id,
                source=dec_out.source,
                status="no_decision",
                explanation="This run has no intervention decision yet.",
            )

        claim_id = input.claim_id or decision.primary_claim_id
        if not claim_id:
            return VisualSummaryOutput(
                run_id=dec_out.run_id,
                source=dec_out.source,
                status="no_decision",
                decision=decision,
                explanation="Intervention decision lacks a primary claim.",
            )

        # If planner already produced a card, reuse it instead of re-rendering.
        if dec_out.visual_card_path:
            return VisualSummaryOutput(
                run_id=dec_out.run_id,
                source=dec_out.source,
                status="rendered",
                image_path=dec_out.visual_card_path,
                decision=decision,
                claim_id=claim_id,
                claim_text=decision.primary_claim_text,
                explanation=(
                    decision.explanation
                    or "Showing the card produced during precompute."
                ),
            )

        # Otherwise render live via visual tools.
        details = get_claim_details(
            GetClaimDetailsInput(run_id=input.run_id, claim_id=claim_id)
        )
        claim = details.claim

        if decision.decision == "rebut":
            counter = dec_out.counter_message
            if not counter:
                return VisualSummaryOutput(
                    run_id=dec_out.run_id,
                    source=dec_out.source,
                    status="insufficient_data",
                    decision=decision,
                    claim_id=claim_id,
                    claim_text=claim.normalized_text,
                    explanation="No counter_message is stored for this run.",
                    reason=dec_out.counter_message_skip_reason,
                )
            payload = ClaimPayload(
                id=claim.claim_id,
                normalized_text=claim.normalized_text,
                non_actionable_reason=claim.non_actionable_reason,
                supporting_evidence=[
                    ev.model_dump(mode="json")
                    for ev in claim.supporting_evidence
                ],
            )
            card = generate_clarification_card(
                GenerateClarificationCardInput(
                    counter_message=counter,
                    claim=payload,
                )
            )
            return VisualSummaryOutput(
                run_id=dec_out.run_id,
                source=dec_out.source,
                status="rendered" if card.path else "render_failed",
                image_path=card.path,
                decision=decision,
                claim_id=claim_id,
                claim_text=claim.normalized_text,
                explanation=decision.explanation,
                reason=card.reason,
            )

        if decision.decision == "evidence_context":
            supporting_count = len(claim.supporting_evidence)
            if supporting_count < 2:
                return VisualSummaryOutput(
                    run_id=dec_out.run_id,
                    source=dec_out.source,
                    status="insufficient_data",
                    decision=decision,
                    claim_id=claim_id,
                    claim_text=claim.normalized_text,
                    explanation=(
                        "Evidence/Context card needs ≥2 supporting facts; "
                        f"this claim has {supporting_count}."
                    ),
                )
            payload = ClaimPayload(
                id=claim.claim_id,
                normalized_text=claim.normalized_text,
                non_actionable_reason=claim.non_actionable_reason,
                supporting_evidence=[
                    ev.model_dump(mode="json")
                    for ev in claim.supporting_evidence
                ],
            )
            card = generate_evidence_context_card(
                GenerateEvidenceContextCardInput(claim=payload)
            )
            return VisualSummaryOutput(
                run_id=dec_out.run_id,
                source=dec_out.source,
                status="rendered" if card.path else "render_failed",
                image_path=card.path,
                decision=decision,
                claim_id=claim_id,
                claim_text=claim.normalized_text,
                explanation=decision.explanation,
                reason=card.reason,
            )

        # abstain (or unknown)
        return VisualSummaryOutput(
            run_id=dec_out.run_id,
            source=dec_out.source,
            status="abstained",
            decision=decision,
            claim_id=claim_id,
            claim_text=claim.normalized_text,
            explanation=(
                decision.explanation
                or "Precompute pipeline abstained from rendering a card."
            ),
            reason=decision.reason,
        )


register_capability(VisualSummaryCapability())
