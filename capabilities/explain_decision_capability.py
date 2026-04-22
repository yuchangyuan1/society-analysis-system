"""ExplainDecisionCapability — 'why did we (not) intervene on this claim?'

Surfaces the intervention_decision block in natural-language-ready form,
plus any counter-effect history for the topic if known.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from capabilities.base import (
    Capability, CapabilityInput, CapabilityOutput, register_capability,
)
from tools.decision_tools import (
    get_intervention_decision, GetInterventionDecisionInput,
    InterventionDecisionView,
    get_counter_effect_history, GetCounterEffectHistoryInput,
    CounterEffectBrief,
)


class ExplainDecisionInput(CapabilityInput):
    run_id: str = "latest"
    topic_id: Optional[str] = None  # if set, include counter-effect history


class ExplainDecisionOutput(CapabilityOutput):
    run_id: str
    source: str
    decision: Optional[InterventionDecisionView] = None
    counter_message: Optional[str] = None
    counter_message_skip_reason: Optional[str] = None
    visual_card_path: Optional[str] = None
    history: list[CounterEffectBrief] = Field(default_factory=list)


class ExplainDecisionCapability(Capability):
    name = "explain_decision"
    description = (
        "Explain the intervention decision for a run or topic: which action "
        "(rebut / clarify / abstain) was taken, why, any skip reasons, and "
        "historical counter-effect scores. Answers 'why did we intervene', "
        "'why no counter-message', 'explain the decision'."
    )
    example_utterances = [
        "为什么干预这个话题",
        "why didn't we post a counter message",
        "explain the decision",
        "为什么没出反驳卡",
    ]
    tags = ["decision", "intervention", "explain"]
    Input = ExplainDecisionInput
    Output = ExplainDecisionOutput

    def run(self, input: ExplainDecisionInput) -> ExplainDecisionOutput:
        dec = get_intervention_decision(
            GetInterventionDecisionInput(run_id=input.run_id)
        )

        history: list[CounterEffectBrief] = []
        if input.topic_id:
            try:
                history = get_counter_effect_history(
                    GetCounterEffectHistoryInput(topic_id=input.topic_id)
                ).records
            except Exception:  # noqa: BLE001 — history is best-effort
                history = []

        return ExplainDecisionOutput(
            run_id=dec.run_id,
            source=dec.source,
            decision=dec.decision,
            counter_message=dec.counter_message,
            counter_message_skip_reason=dec.counter_message_skip_reason,
            visual_card_path=dec.visual_card_path,
            history=history,
        )


register_capability(ExplainDecisionCapability())
