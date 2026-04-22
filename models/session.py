"""Chat session state model.

First version uses flat JSON files at `data/sessions/{session_id}.json`
(see `services/session_store.py`). No DB, no concurrency locks — single
user, single machine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ConversationTurn(BaseModel):
    """One turn of the chat conversation."""
    role: str                            # "user" | "assistant"
    content: str
    capability_used: Optional[str] = None
    at: datetime = Field(default_factory=datetime.utcnow)


class SessionState(BaseModel):
    """Persisted per-session state.

    Tracks which run / topic / claim the user is currently asking about
    so that follow-up questions can inherit context without the user
    having to re-specify. `recent_visuals` lets the UI show thumbnails
    of visuals the user generated earlier in the conversation.
    """
    session_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    current_run_id: Optional[str] = None
    current_topic_id: Optional[str] = None
    current_claim_id: Optional[str] = None
    recent_visuals: list[str] = Field(default_factory=list)
    conversation: list[ConversationTurn] = Field(default_factory=list)
