"""Chat Orchestrator — the single entry point for `POST /chat/query`.

Pipeline (per interactive_agent_transformation_plan_skills_mcp.md §7.3):

    load session state
      → router.classify(message, session_context)
      → capability = CAPABILITY_REGISTRY[intent]
      → capability.run(input)
      → answer_composer.compose(...)
      → append turns to session state
      → return ChatResponse
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from pydantic import BaseModel

# Import capabilities package to populate CAPABILITY_REGISTRY.
import capabilities  # noqa: F401 — side-effect: registers capabilities
from capabilities import CAPABILITY_REGISTRY
from capabilities.base import CapabilityError

# Ensure concrete capabilities are imported too (registration happens on
# module import).
import capabilities.topic_overview_capability  # noqa: F401
import capabilities.emotion_insight_capability  # noqa: F401
import capabilities.claim_status_capability  # noqa: F401
import capabilities.propagation_insight_capability  # noqa: F401
import capabilities.visual_summary_capability  # noqa: F401
import capabilities.run_compare_capability  # noqa: F401
import capabilities.explain_decision_capability  # noqa: F401

from agents.router import IntentRouter, RouterOutput
from models.chat import ChatResponse
from models.session import SessionState
from services import session_store
from services.answer_composer import AnswerComposer


class ChatOrchestrator:
    def __init__(
        self,
        router: Optional[IntentRouter] = None,
        composer: Optional[AnswerComposer] = None,
    ) -> None:
        self._router = router or IntentRouter()
        self._composer = composer or AnswerComposer()

    # ── Public entry point ────────────────────────────────────────────────

    def handle(self, session_id: str, message: str) -> ChatResponse:
        state = session_store.load(session_id or _new_session_id())
        session_context = _session_context(state)

        session_store.append_turn(state, role="user", content=message)

        route = self._router.classify(message, session_context)
        capability_output, cap_name, visuals = self._dispatch(route, state)

        answer_text = self._composer.compose(
            user_message=message,
            capability_name=cap_name,
            capability_output=capability_output,
            session_context=session_context,
        )

        session_store.append_turn(
            state, role="assistant", content=answer_text,
            capability_used=cap_name,
        )
        self._update_session_targets(state, route, capability_output)
        session_store.save(state)

        return ChatResponse(
            session_id=state.session_id,
            answer_text=answer_text,
            capability_used=cap_name,
            capability_output=capability_output,
            visual_paths=visuals,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _dispatch(
        self, route: RouterOutput, state: SessionState,
    ) -> tuple[Optional[dict[str, Any]], Optional[str], list[str]]:
        intent = route.intent
        if intent == "followup":
            intent = _resolve_followup(state)

        if intent == "other" or intent not in CAPABILITY_REGISTRY:
            return None, None, []

        capability = CAPABILITY_REGISTRY[intent]
        try:
            payload = _build_capability_input(capability, route, state)
            output = capability.run(payload)
            return (
                output.model_dump(mode="json"),
                capability.name,
                _extract_visuals(output),
            )
        except CapabilityError as exc:
            return {"error": str(exc)}, capability.name, []
        except Exception as exc:
            return (
                {"error": f"{type(exc).__name__}: {exc}"},
                capability.name,
                [],
            )

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


def _resolve_followup(state: SessionState) -> str:
    """For followup intent, inherit the most recent capability."""
    for t in reversed(state.conversation):
        if t.capability_used:
            return t.capability_used
    return "other"


def _build_capability_input(
    capability, route: RouterOutput, state: SessionState
) -> BaseModel:
    """Construct Input model for a capability using router targets + session."""
    cls = capability.Input
    fields = set(cls.model_fields.keys())

    run_id = route.targets.run_id or state.current_run_id or "latest"
    topic_id = route.targets.topic_id or state.current_topic_id
    claim_id = route.targets.claim_id or state.current_claim_id

    payload: dict[str, Any] = {}
    if "run_id" in fields:
        payload["run_id"] = run_id
    if "topic_id" in fields and topic_id is not None:
        payload["topic_id"] = topic_id
    if "claim_id" in fields and claim_id is not None:
        payload["claim_id"] = claim_id
    # run_compare: baseline_run_id is always inferred (capability picks prev).
    if "baseline_run_id" in fields:
        payload["baseline_run_id"] = None
    return cls(**payload)


def _extract_visuals(output: BaseModel) -> list[str]:
    """Best-effort visual path extraction from capability output."""
    data = output.model_dump(mode="json")
    paths: list[str] = []
    for key in (
        "image_path", "visual_path", "visual_card_path", "counter_visuals",
    ):
        v = data.get(key)
        if isinstance(v, str):
            paths.append(v)
        elif isinstance(v, list):
            paths.extend(p for p in v if isinstance(p, str))
    return paths
