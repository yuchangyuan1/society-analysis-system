"""RunCompareCapability — 'how does this run compare to the last one?'

Reads manifest + metrics + propagation_summary for two runs and diffs the
interesting numeric fields. No LLM call — AnswerComposer renders narrative.

If baseline_run_id is not given, defaults to the run immediately before
target in `list_runs` ordering.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from capabilities.base import (
    Capability, CapabilityInput, CapabilityOutput,
    CapabilityError, register_capability,
)
from tools.run_query_tools import (
    list_runs, ListRunsInput,
    get_run_summary, GetRunSummaryInput,
)
from tools.graph_tools import (
    get_propagation_summary, GetPropagationSummaryInput,
    get_social_metrics, GetSocialMetricsInput,
)


class RunCompareInput(CapabilityInput):
    run_id: str = "latest"
    baseline_run_id: Optional[str] = None


class FieldChange(BaseModel):
    field: str
    baseline: Optional[float] = None
    target: Optional[float] = None
    delta: Optional[float] = None
    direction: str = "flat"  # "up" | "down" | "flat" | "unknown"


class RunCompareOutput(CapabilityOutput):
    target_run_id: str
    baseline_run_id: str
    target_source: str
    baseline_source: str
    changes: list[FieldChange] = Field(default_factory=list)
    target_query: Optional[str] = None
    baseline_query: Optional[str] = None


_COMPARE_FIELDS_METRICS = [
    "evidence_coverage",
    "community_modularity_q",
    "counter_effect_closed_loop_rate",
    "bridge_influence_ratio",
    "misinfo_risk_p95",
]

_COMPARE_FIELDS_PROPAGATION = [
    "post_count",
    "unique_accounts",
    "velocity",
    "coordinated_pairs",
]


def _direction(baseline: Optional[float], target: Optional[float]) -> str:
    if baseline is None or target is None:
        return "unknown"
    if target > baseline:
        return "up"
    if target < baseline:
        return "down"
    return "flat"


def _diff(
    field: str, baseline: Optional[float], target: Optional[float]
) -> FieldChange:
    delta: Optional[float] = None
    if baseline is not None and target is not None:
        delta = round(target - baseline, 4)
    return FieldChange(
        field=field,
        baseline=baseline,
        target=target,
        delta=delta,
        direction=_direction(baseline, target),
    )


def _pick_baseline(target_run_id: str) -> str:
    """Find the run right before target in reverse-alpha order.

    Uses list_runs (data + samples). Raises if fewer than 2 runs exist.
    """
    runs = list_runs(ListRunsInput(include_samples=True)).runs
    ids = [r.run_id for r in runs]
    if target_run_id == "latest":
        if len(ids) < 2:
            raise CapabilityError("need at least 2 runs to compare")
        # list_runs returns newest first → index 0 is latest, 1 is baseline
        return ids[1]
    try:
        idx = ids.index(target_run_id)
    except ValueError as exc:
        raise CapabilityError(
            f"target run_id not found: {target_run_id}"
        ) from exc
    if idx + 1 >= len(ids):
        raise CapabilityError(
            f"no baseline found older than {target_run_id}"
        )
    return ids[idx + 1]


class RunCompareCapability(Capability):
    name = "run_compare"
    description = (
        "Compare two runs (typically the latest vs the previous) on "
        "post_count / unique_accounts / velocity / misinfo_risk / counter "
        "messages. Emit a list of deltas + narrative. Answers 'how did "
        "things change', 'compare to yesterday', 'what's different this run'."
    )
    example_utterances = [
        "这次和上次比变化大吗",
        "compare to last run",
        "和昨天比怎么样",
        "what's different from the previous run",
    ]
    tags = ["compare", "delta", "metrics"]
    Input = RunCompareInput
    Output = RunCompareOutput

    def run(self, input: RunCompareInput) -> RunCompareOutput:
        baseline_id = input.baseline_run_id or _pick_baseline(input.run_id)

        target = get_run_summary(GetRunSummaryInput(run_id=input.run_id))
        baseline = get_run_summary(GetRunSummaryInput(run_id=baseline_id))

        target_metrics = target.metrics or {}
        baseline_metrics = baseline.metrics or {}

        # Propagation summaries live in report_raw.json; re-use graph tool.
        target_prop = get_propagation_summary(
            GetPropagationSummaryInput(run_id=input.run_id)
        ).propagation_summary
        baseline_prop = get_propagation_summary(
            GetPropagationSummaryInput(run_id=baseline_id)
        ).propagation_summary

        # Some metrics (bridge_influence_ratio) may live in either file.
        target_social = get_social_metrics(
            GetSocialMetricsInput(run_id=input.run_id)
        ).metrics or {}
        baseline_social = get_social_metrics(
            GetSocialMetricsInput(run_id=baseline_id)
        ).metrics or {}

        changes: list[FieldChange] = []

        for field in _COMPARE_FIELDS_METRICS:
            b = baseline_metrics.get(field)
            if b is None:
                b = baseline_social.get(field)
            t = target_metrics.get(field)
            if t is None:
                t = target_social.get(field)
            if b is None and t is None:
                continue
            changes.append(_diff(field, _as_float(b), _as_float(t)))

        for field in _COMPARE_FIELDS_PROPAGATION:
            b = baseline_prop.get(field)
            t = target_prop.get(field)
            if b is None and t is None:
                continue
            changes.append(_diff(field, _as_float(b), _as_float(t)))

        return RunCompareOutput(
            target_run_id=target.run_id,
            baseline_run_id=baseline.run_id,
            target_source=target.source,
            baseline_source=baseline.source,
            changes=changes,
            target_query=target.manifest.get("query_text"),
            baseline_query=baseline.manifest.get("query_text"),
        )


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


register_capability(RunCompareCapability())
