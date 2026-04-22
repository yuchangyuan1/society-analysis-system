"""Tools for intervention decisions + counter-message effectiveness history.

`get_intervention_decision` — reads the `intervention_decision` block from
report_raw.json (populated by the planner after primary_claim selection).
`get_counter_effect_history` — queries CounterEffectService for past
deployments matching the topic/claim at hand.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from tools.base import ToolInput, ToolOutput, ToolInputError
from tools.graph_tools import _load_raw
from tools.run_query_tools import _resolve_run_dir, Source


# ─── Lazy singleton ──────────────────────────────────────────────────────────

_counter = None


def _get_counter_effect():
    global _counter
    if _counter is None:
        from services.counter_effect_service import CounterEffectService
        _counter = CounterEffectService()
    return _counter


# ─── Models ──────────────────────────────────────────────────────────────────

class InterventionDecisionView(BaseModel):
    primary_claim_id: Optional[str] = None
    primary_claim_text: Optional[str] = None
    decision: Optional[str] = None  # "rebut" | "evidence_context" | "abstain"
    reason: Optional[str] = None
    explanation: str = ""
    recommended_next_step: str = ""
    visual_type: Optional[str] = None


class GetInterventionDecisionInput(ToolInput):
    run_id: str = "latest"


class GetInterventionDecisionOutput(ToolOutput):
    run_id: str
    source: Source
    decision: Optional[InterventionDecisionView] = None
    counter_message: Optional[str] = None
    counter_message_skip_reason: Optional[str] = None
    visual_card_path: Optional[str] = None


class CounterEffectBrief(BaseModel):
    record_id: str
    report_id: str
    topic_id: Optional[str] = None
    topic_label: Optional[str] = None
    claim_id: Optional[str] = None
    counter_message: str = ""
    deployed_at: Optional[str] = None
    baseline_velocity: float = 0.0
    followup_velocity: Optional[float] = None
    outcome: Optional[str] = None
    effect_score: Optional[float] = None


class GetCounterEffectHistoryInput(ToolInput):
    topic_id: Optional[str] = None
    claim_id: Optional[str] = None
    limit: int = 10


class GetCounterEffectHistoryOutput(ToolOutput):
    records: list[CounterEffectBrief] = Field(default_factory=list)


# ─── Tool functions ──────────────────────────────────────────────────────────

def get_intervention_decision(
    input: GetInterventionDecisionInput,
) -> GetInterventionDecisionOutput:
    """Return intervention_decision + counter_message + visual_card_path."""
    run_dir, source = _resolve_run_dir(input.run_id)
    raw = _load_raw(run_dir)

    dec_raw = raw.get("intervention_decision")
    decision = None
    if dec_raw:
        decision = InterventionDecisionView(
            primary_claim_id=dec_raw.get("primary_claim_id"),
            primary_claim_text=dec_raw.get("primary_claim_text"),
            decision=dec_raw.get("decision"),
            reason=dec_raw.get("reason"),
            explanation=dec_raw.get("explanation", ""),
            recommended_next_step=dec_raw.get("recommended_next_step", ""),
            visual_type=dec_raw.get("visual_type"),
        )

    return GetInterventionDecisionOutput(
        run_id=run_dir.name,
        source=source,
        decision=decision,
        counter_message=raw.get("counter_message"),
        counter_message_skip_reason=raw.get("counter_message_skip_reason"),
        visual_card_path=raw.get("visual_card_path"),
    )


def _brief_from_record(rec: Any) -> CounterEffectBrief:
    return CounterEffectBrief(
        record_id=rec.record_id,
        report_id=rec.report_id,
        topic_id=rec.topic_id,
        topic_label=rec.topic_label,
        claim_id=rec.claim_id,
        counter_message=rec.counter_message or "",
        deployed_at=rec.deployed_at.isoformat() if rec.deployed_at else None,
        baseline_velocity=rec.baseline_velocity,
        followup_velocity=rec.followup_velocity,
        outcome=getattr(rec, "outcome", None),
        effect_score=getattr(rec, "effect_score", None),
    )


def get_counter_effect_history(
    input: GetCounterEffectHistoryInput,
) -> GetCounterEffectHistoryOutput:
    """Return counter-effect records for a topic_id or (best-effort) claim_id."""
    if not input.topic_id and not input.claim_id:
        raise ToolInputError("topic_id or claim_id required")

    svc = _get_counter_effect()
    records: list[Any] = []
    if input.topic_id:
        records = svc.get_records_by_topic(input.topic_id)
    else:
        pending = svc.get_pending_by_keys(claim_ids=[input.claim_id or ""])
        records = list(pending)

    records = records[: input.limit]
    return GetCounterEffectHistoryOutput(
        records=[_brief_from_record(r) for r in records],
    )
