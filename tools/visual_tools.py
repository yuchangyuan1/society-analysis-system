"""Thin wrappers around VisualAgent for chat-time card generation.

These tools are stateless; they build a VisualAgent lazily. Capabilities use
them to render a clarification or evidence-context card on demand. The tools
do NOT decide whether to render — that lives in VisualSummaryCapability.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from tools.base import ToolInput, ToolOutput, ToolError


# ─── Lazy singleton ──────────────────────────────────────────────────────────

_visual_agent = None


def _get_visual_agent():
    global _visual_agent
    if _visual_agent is None:
        from services.stable_diffusion_service import StableDiffusionService
        from agents.visual import VisualAgent
        _visual_agent = VisualAgent(sd=StableDiffusionService())
    return _visual_agent


# ─── Models ──────────────────────────────────────────────────────────────────

class ClaimPayload(BaseModel):
    """Minimal claim shape accepted by visual_tools.

    We don't re-export models.claim.Claim to keep tools independent — the
    VisualAgent needs a subset of Claim fields, which we rebuild locally.
    """
    id: str
    normalized_text: str
    non_actionable_reason: Optional[str] = None
    supporting_evidence: list[dict] = []


class GenerateClarificationCardInput(ToolInput):
    counter_message: str
    claim: ClaimPayload
    report_id: str = ""


class GenerateEvidenceContextCardInput(ToolInput):
    claim: ClaimPayload
    report_id: str = ""


class CardOutput(ToolOutput):
    path: Optional[str] = None
    reason: Optional[str] = None  # populated when path is None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _to_claim(payload: ClaimPayload):
    """Rebuild a real models.claim.Claim for VisualAgent."""
    from models.claim import Claim, ClaimEvidence
    evidence = [ClaimEvidence(**ev) for ev in payload.supporting_evidence]
    return Claim(
        id=payload.id,
        normalized_text=payload.normalized_text,
        non_actionable_reason=payload.non_actionable_reason,
        supporting_evidence=evidence,
    )


# ─── Tool functions ──────────────────────────────────────────────────────────

def generate_clarification_card(
    input: GenerateClarificationCardInput,
) -> CardOutput:
    """Render a rebuttal-style card. Returns path=None if SD unavailable."""
    try:
        agent = _get_visual_agent()
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"visual agent init failed: {exc}") from exc

    try:
        path = agent.generate_clarification_card(
            counter_message=input.counter_message,
            claim=_to_claim(input.claim),
            report_id=input.report_id,
        )
    except Exception as exc:  # noqa: BLE001
        return CardOutput(path=None, reason=f"render_error: {exc}")

    if path is None:
        return CardOutput(path=None, reason="sd_unavailable")
    return CardOutput(path=path)


def generate_evidence_context_card(
    input: GenerateEvidenceContextCardInput,
) -> CardOutput:
    """Render a two-column Evidence/Context card (for non_actionable claims)."""
    try:
        agent = _get_visual_agent()
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"visual agent init failed: {exc}") from exc

    try:
        path = agent.generate_evidence_context_card(
            claim=_to_claim(input.claim),
            report_id=input.report_id,
        )
    except Exception as exc:  # noqa: BLE001
        return CardOutput(path=None, reason=f"render_error: {exc}")

    if path is None:
        return CardOutput(path=None, reason="no_supporting_evidence")
    return CardOutput(path=path)
