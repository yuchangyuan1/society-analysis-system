"""Online Query Planner — bounded DAG workflow planner.

Given a Router classification of a user message, this Planner:
  1. looks up one of 7 pre-defined `WorkflowTemplate`s,
  2. binds parameters from router targets + session state,
  3. executes each step (Capability call) in order,
  4. merges intermediate outputs into a `PlanExecution`.

**Bounded by design** (per `complete_project_transformation_plan.md` §4.2):
- Templates are hard-coded — the planner does NOT synthesize arbitrary DAGs
  at runtime. LLM is only used in (a) each Capability's internal reasoning
  and (b) the final AnswerComposer.
- Every template has ≤ 4 capability steps (hard cap).
- No retries, no self-reflection loops, no unbounded tool use.

This buys us: reproducibility, latency control (10-30s target), easy
debugging, and evaluation stability.

The Planner is a step up from single-capability dispatch because some user
questions require composing multiple capabilities (e.g. a claim verification
that also wants the propagation picture uses `evidence_comparison_flow` =
claim_status + propagation_analysis).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import structlog
from pydantic import BaseModel

# Ensure capability modules register themselves at import time.
import capabilities  # noqa: F401
import capabilities.topic_overview_capability  # noqa: F401
import capabilities.emotion_insight_capability  # noqa: F401
import capabilities.claim_status_capability  # noqa: F401
import capabilities.propagation_insight_capability  # noqa: F401
import capabilities.visual_summary_capability  # noqa: F401
import capabilities.run_compare_capability  # noqa: F401
import capabilities.explain_decision_capability  # noqa: F401

from capabilities.base import CAPABILITY_REGISTRY, CapabilityError
from agents.router import RouterOutput
from models.session import SessionState

log = structlog.get_logger(__name__)


# ─── Data contracts ──────────────────────────────────────────────────────────

@dataclass
class WorkflowStep:
    """One node in a workflow DAG."""

    capability_name: str
    # Build the capability Input from (route, session, prior_outputs).
    input_builder: Callable[
        [RouterOutput, SessionState, dict[str, Any]], dict[str, Any]
    ]
    # Optional gate: returns False to skip this step.
    condition: Optional[
        Callable[[RouterOutput, SessionState, dict[str, Any]], bool]
    ] = None
    # Optional display alias when multiple steps call the same capability.
    alias: Optional[str] = None

    @property
    def key(self) -> str:
        return self.alias or self.capability_name


@dataclass
class WorkflowTemplate:
    """One bounded DAG template selectable by the planner."""

    name: str
    description: str
    intent: str  # matches RouterIntent literal
    steps: list[WorkflowStep] = field(default_factory=list)


class StepResult(BaseModel):
    capability: str
    alias: str
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    visual_paths: list[str] = []


class PlanExecution(BaseModel):
    workflow_name: str
    intent: str
    steps: list[StepResult]
    primary_output: Optional[dict[str, Any]] = None
    primary_capability: Optional[str] = None
    visual_paths: list[str] = []


# ─── Input builders (pure functions) ─────────────────────────────────────────

def _run_id(route: RouterOutput, state: SessionState) -> str:
    return route.targets.run_id or state.current_run_id or "latest"


def _topic_id(route: RouterOutput, state: SessionState) -> Optional[str]:
    return route.targets.topic_id or state.current_topic_id


def _claim_id(route: RouterOutput, state: SessionState) -> Optional[str]:
    return route.targets.claim_id or state.current_claim_id


def _topic_overview_input(route, state, _prior) -> dict[str, Any]:
    return {"run_id": _run_id(route, state)}


def _emotion_input(route, state, _prior) -> dict[str, Any]:
    payload: dict[str, Any] = {"run_id": _run_id(route, state)}
    topic_id = _topic_id(route, state)
    if topic_id:
        payload["topic_id"] = topic_id
    return payload


def _propagation_input(route, state, _prior) -> dict[str, Any]:
    payload: dict[str, Any] = {"run_id": _run_id(route, state)}
    topic_id = _topic_id(route, state)
    if topic_id:
        payload["topic_id"] = topic_id
    return payload


def _claim_status_input(route, state, _prior) -> dict[str, Any]:
    payload: dict[str, Any] = {"run_id": _run_id(route, state)}
    claim_id = _claim_id(route, state)
    if claim_id:
        payload["claim_id"] = claim_id
    return payload


def _visual_input(route, state, prior) -> dict[str, Any]:
    payload: dict[str, Any] = {"run_id": _run_id(route, state)}
    topic_id = _topic_id(route, state)
    claim_id = _claim_id(route, state)
    if topic_id:
        payload["topic_id"] = topic_id
    if claim_id:
        payload["claim_id"] = claim_id
    return payload


def _run_compare_input(route, state, _prior) -> dict[str, Any]:
    return {
        "run_id": _run_id(route, state),
        "baseline_run_id": None,
    }


def _explain_decision_input(route, state, _prior) -> dict[str, Any]:
    payload: dict[str, Any] = {"run_id": _run_id(route, state)}
    topic_id = _topic_id(route, state)
    if topic_id:
        payload["topic_id"] = topic_id
    return payload


# ─── Condition gates ─────────────────────────────────────────────────────────

def _has_topic_context(
    route: RouterOutput, state: SessionState, _prior: dict[str, Any]
) -> bool:
    """Chain propagation only if we have a topic anchor (from router or prior)."""
    if _topic_id(route, state):
        return True
    cs = _prior.get("claim_status") or {}
    return bool(cs.get("topic_id"))


# ─── Templates ───────────────────────────────────────────────────────────────
# Each Router intent has exactly one primary template. Some templates chain
# multiple capabilities so the Composer gets a richer context.


TEMPLATES: dict[str, WorkflowTemplate] = {
    "topic_overview": WorkflowTemplate(
        name="topic_overview_flow",
        description="List the top-K trending topics of a run.",
        intent="topic_overview",
        steps=[
            WorkflowStep("topic_overview", _topic_overview_input),
        ],
    ),
    "emotion_analysis": WorkflowTemplate(
        name="emotion_insight_flow",
        description="Emotion distribution + interpretation for a run or topic.",
        intent="emotion_analysis",
        steps=[
            WorkflowStep("emotion_analysis", _emotion_input),
        ],
    ),
    "propagation_analysis": WorkflowTemplate(
        name="propagation_analysis_flow",
        description="Kuzu graph lookup → bridge accounts, communities, velocity.",
        intent="propagation_analysis",
        steps=[
            WorkflowStep("propagation_analysis", _propagation_input),
        ],
    ),
    # Claim verification is the canonical multi-step workflow — the user
    # asks "is X true?" and the Planner also attaches propagation context so
    # the Composer can explain *who* is spreading it.
    "claim_status": WorkflowTemplate(
        name="claim_verification_flow",
        description=(
            "Fact-check a claim (5-tier verdict + citations) and attach the "
            "propagation picture of the same topic."
        ),
        intent="claim_status",
        steps=[
            WorkflowStep("claim_status", _claim_status_input),
            WorkflowStep(
                "propagation_analysis",
                _propagation_input,
                condition=_has_topic_context,
                alias="propagation_context",
            ),
        ],
    ),
    "visual_summary": WorkflowTemplate(
        name="visual_summary_flow",
        description=(
            "Generate a Clarification / Evidence / abstention_block card for "
            "a run or claim. Decision precedes visual."
        ),
        intent="visual_summary",
        steps=[
            WorkflowStep("visual_summary", _visual_input),
        ],
    ),
    "run_compare": WorkflowTemplate(
        name="run_comparison_flow",
        description="Delta metrics + narrative between the latest and prior run.",
        intent="run_compare",
        steps=[
            WorkflowStep("run_compare", _run_compare_input),
        ],
    ),
    "explain_decision": WorkflowTemplate(
        name="decision_explanation_flow",
        description="Explain the intervention decision + show counter-effect history.",
        intent="explain_decision",
        steps=[
            WorkflowStep("explain_decision", _explain_decision_input),
        ],
    ),
}


# ─── Planner Agent ───────────────────────────────────────────────────────────

class PlannerAgent:
    """Online query planner. Maps Router intent → bounded DAG → execution.

    The Planner does NOT import services directly and does NOT read
    `data/runs/*.json`. All data access happens inside Capability → Tool.
    """

    # Hard safety cap: no workflow may exceed this many capability steps.
    MAX_STEPS_PER_WORKFLOW = 4

    def __init__(self) -> None:
        for tpl in TEMPLATES.values():
            if len(tpl.steps) > self.MAX_STEPS_PER_WORKFLOW:
                raise ValueError(
                    f"Template {tpl.name} has {len(tpl.steps)} steps "
                    f"(> MAX {self.MAX_STEPS_PER_WORKFLOW})"
                )

    # ---- Public API ----------------------------------------------------------

    def plan(self, route: RouterOutput) -> Optional[WorkflowTemplate]:
        """Pick a workflow template for the given Router output. Deterministic."""
        template = TEMPLATES.get(route.intent)
        if template is None:
            log.info("planner.no_template", intent=route.intent)
        return template

    def execute(
        self, template: WorkflowTemplate, route: RouterOutput, state: SessionState,
    ) -> PlanExecution:
        """Run each step in sequence, passing outputs forward."""
        prior: dict[str, Any] = {}
        step_results: list[StepResult] = []
        visuals: list[str] = []

        log.info(
            "planner.execute.start",
            workflow=template.name,
            steps=[s.capability_name for s in template.steps],
        )

        for step in template.steps:
            # Conditional skip
            if step.condition is not None and not step.condition(route, state, prior):
                log.info(
                    "planner.step.skipped",
                    workflow=template.name,
                    step=step.key,
                    reason="condition_false",
                )
                continue

            capability = CAPABILITY_REGISTRY.get(step.capability_name)
            if capability is None:
                step_results.append(StepResult(
                    capability=step.capability_name,
                    alias=step.key,
                    error="capability_not_registered",
                ))
                continue

            try:
                payload = step.input_builder(route, state, prior)
                input_obj = capability.Input(**payload)
                output_obj = capability.run(input_obj)
                output_dict = output_obj.model_dump(mode="json")

                step_visuals = _extract_visuals(output_dict)
                visuals.extend(step_visuals)

                step_results.append(StepResult(
                    capability=step.capability_name,
                    alias=step.key,
                    output=output_dict,
                    visual_paths=step_visuals,
                ))
                prior[step.key] = output_dict

            except CapabilityError as exc:
                log.warning(
                    "planner.step.capability_error",
                    workflow=template.name, step=step.key, error=str(exc),
                )
                step_results.append(StepResult(
                    capability=step.capability_name,
                    alias=step.key,
                    error=f"CapabilityError: {exc}",
                ))
            except Exception as exc:
                log.error(
                    "planner.step.error",
                    workflow=template.name, step=step.key,
                    error=f"{type(exc).__name__}: {exc}",
                )
                step_results.append(StepResult(
                    capability=step.capability_name,
                    alias=step.key,
                    error=f"{type(exc).__name__}: {exc}",
                ))

        # The first successful step is the "primary" output used by the
        # AnswerComposer as the main answer signal.
        primary_step = next((s for s in step_results if s.output is not None), None)

        log.info(
            "planner.execute.done",
            workflow=template.name,
            steps_run=len(step_results),
            primary=(primary_step.alias if primary_step else None),
        )

        return PlanExecution(
            workflow_name=template.name,
            intent=template.intent,
            steps=step_results,
            primary_output=(primary_step.output if primary_step else None),
            primary_capability=(primary_step.capability if primary_step else None),
            visual_paths=visuals,
        )

    def plan_and_execute(
        self, route: RouterOutput, state: SessionState,
    ) -> Optional[PlanExecution]:
        """Convenience: plan + execute in one call. Returns None if no template."""
        template = self.plan(route)
        if template is None:
            return None
        return self.execute(template, route, state)

    # ---- Introspection (for debug / UI) -------------------------------------

    @staticmethod
    def templates() -> list[WorkflowTemplate]:
        return list(TEMPLATES.values())


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _extract_visuals(output_dict: dict[str, Any]) -> list[str]:
    """Best-effort visual path extraction from a capability output dict."""
    paths: list[str] = []
    for key in ("image_path", "visual_path", "visual_card_path", "counter_visuals"):
        v = output_dict.get(key)
        if isinstance(v, str):
            paths.append(v)
        elif isinstance(v, list):
            paths.extend(p for p in v if isinstance(p, str))
    return paths
