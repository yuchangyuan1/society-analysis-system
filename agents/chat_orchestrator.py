"""Chat Orchestrator — single entry point for `POST /chat/query`.

Pipeline (per `complete_project_transformation_plan.md` §4.2-4.4):

    load session state
      → router.classify(message, session_context)     # intent + targets
      → planner.plan(route)                           # pick workflow template
      → planner.execute(template, route, state)       # run bounded DAG
      → answer_composer.compose(...)                  # natural-language answer
      → append turns to session state
      → return ChatResponse

The Orchestrator itself contains **zero** business logic — it only wires
Router → Planner → Composer and maintains session state. Capabilities stay
unaware of conversation history; Tools stay unaware of sessions entirely.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

# Ensure capability modules register themselves.
import capabilities  # noqa: F401
import capabilities.topic_overview_capability  # noqa: F401
import capabilities.emotion_insight_capability  # noqa: F401
import capabilities.claim_status_capability  # noqa: F401
import capabilities.propagation_insight_capability  # noqa: F401
import capabilities.visual_summary_capability  # noqa: F401
import capabilities.run_compare_capability  # noqa: F401
import capabilities.explain_decision_capability  # noqa: F401

from agents.planner import PlannerAgent, PlanExecution
from agents.router import IntentRouter, RouterOutput
from models.chat import ChatResponse
from models.session import SessionState
from services import session_store
from services.answer_composer import AnswerComposer


class ChatOrchestrator:
    def __init__(
        self,
        router: Optional[IntentRouter] = None,
        planner: Optional[PlannerAgent] = None,
        composer: Optional[AnswerComposer] = None,
    ) -> None:
        self._router = router or IntentRouter()
        self._planner = planner or PlannerAgent()
        self._composer = composer or AnswerComposer()

    # ── Public entry point ────────────────────────────────────────────────

    def handle(self, session_id: str, message: str) -> ChatResponse:
        state = session_store.load(session_id or _new_session_id())
        session_context = _session_context(state)

        session_store.append_turn(state, role="user", content=message)

        route = self._router.classify(message, session_context)
        intent = _resolve_intent(route, state)
        route = route.model_copy(update={"intent": intent})

        execution = self._planner.plan_and_execute(route, state)

        capability_name, capability_output, visuals = _unpack(execution)

        answer_text = self._composer.compose(
            user_message=message,
            capability_name=capability_name,
            capability_output=capability_output,
            session_context=session_context,
        )

        session_store.append_turn(
            state, role="assistant", content=answer_text,
            capability_used=capability_name,
        )
        self._update_session_targets(state, route, capability_output)
        session_store.save(state)

        return ChatResponse(
            session_id=state.session_id,
            answer_text=answer_text,
            capability_used=capability_name,
            capability_output=capability_output,
            visual_paths=visuals,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _update_session_targets(
        state: SessionState,
        route: RouterOutput,
        capability_output: Optional[dict[str, Any]],
    ) -> None:
        if route.targets.run_id:
            state.current_run_id = route.targets.run_id
        if capability_output and capability_output.get("run_id"):
            state.current_run_id = capability_output["run_id"]
        if route.targets.topic_id:
            state.current_topic_id = route.targets.topic_id
        if route.targets.claim_id:
            state.current_claim_id = route.targets.claim_id


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _new_session_id() -> str:
    return f"s-{uuid.uuid4().hex[:12]}"


def _session_context(state: SessionState) -> dict[str, Any]:
    return {
        "current_run_id": state.current_run_id,
        "current_topic_id": state.current_topic_id,
        "current_claim_id": state.current_claim_id,
        "recent_capabilities": [
            t.capability_used for t in state.conversation[-4:] if t.capability_used
        ],
    }


def _resolve_intent(route: RouterOutput, state: SessionState) -> str:
    """Followup intent inherits the most recent capability from session."""
    if route.intent != "followup":
        return route.intent
    for turn in reversed(state.conversation):
        if turn.capability_used:
            return turn.capability_used
    return "other"


def _unpack(
    execution: Optional[PlanExecution],
) -> tuple[Optional[str], Optional[dict[str, Any]], list[str]]:
    """Return (capability_name, capability_output, visuals) from execution.

    Shape chosen so the Composer and the HTTP response stay stable whether
    the planner ran a 1-step or multi-step workflow. The primary step drives
    the user-facing answer; secondary steps get attached under
    `aux_outputs` so the UI can still render side panels (graph, etc.).
    """
    if execution is None:
        return None, None, []

    primary = execution.primary_output
    if primary is None:
        # All steps failed. Surface the first error.
        first_err = next(
            (s.error for s in execution.steps if s.error), "no_capability_match"
        )
        return (
            execution.primary_capability,
            {"error": first_err, "workflow": execution.workflow_name},
            execution.visual_paths,
        )

    # Merge secondary steps so the frontend can access them without
    # re-running the workflow.
    aux: dict[str, Any] = {}
    for step in execution.steps:
        if step.alias == execution.primary_capability:
            continue
        if step.output is not None:
            aux[step.alias] = step.output

    enriched = dict(primary)
    if aux:
        enriched["aux_outputs"] = aux
    enriched["workflow"] = execution.workflow_name

    return (
        execution.primary_capability,
        enriched,
        execution.visual_paths,
    )
