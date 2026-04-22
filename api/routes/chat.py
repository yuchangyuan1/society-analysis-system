"""/chat — session-centric conversational entry point.

POST /chat/query     — run one chat turn, return structured + text answer.
GET  /chat/session   — return current session state (for debugging).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from agents.chat_orchestrator import ChatOrchestrator
from models.chat import ChatQuery, ChatResponse
from models.session import SessionState
from services import session_store


router = APIRouter(prefix="/chat", tags=["chat"])

_orchestrator: Optional[ChatOrchestrator] = None


def get_orchestrator() -> ChatOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ChatOrchestrator()
    return _orchestrator


@router.post("/query", response_model=ChatResponse)
def chat_query(
    body: ChatQuery,
    orch: ChatOrchestrator = Depends(get_orchestrator),
) -> ChatResponse:
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is empty")
    return orch.handle(session_id=body.session_id, message=body.message)


@router.get("/session/{session_id}", response_model=SessionState)
def get_session(session_id: str) -> SessionState:
    return session_store.load(session_id)
