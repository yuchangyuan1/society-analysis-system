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
    # redesign-2026-05 Phase 4: branches the v2 path actually invoked
    branches_used: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=datetime.utcnow)


class SessionState(BaseModel):
    """Persisted per-session state.

    Tracks which run / topic / claim the user is currently asking about
    so that follow-up questions can inherit context without the user
    having to re-specify. `recent_visuals` lets the UI show thumbnails
    of visuals the user generated earlier in the conversation.

    Long conversations are managed by Phase 6 context optimisation:
      - `conversation` is hard-trimmed to SESSION_MAX_TURNS (default 40).
      - When trimming, the dropped turns are LLM-compressed into
        `summary` so the Rewriter still sees pre-window context.
      - `summary_until_turn` records up to which absolute turn index
        `summary` covers, so we don't recompress the same turns twice.
    """
    session_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    current_run_id: Optional[str] = None
    current_topic_id: Optional[str] = None
    current_claim_id: Optional[str] = None
    recent_visuals: list[str] = Field(default_factory=list)
    conversation: list[ConversationTurn] = Field(default_factory=list)
    # Phase 6 (A + B):
    summary: str = ""               # rolling LLM summary of pre-window turns
    summary_until_turn: int = 0     # absolute turn index covered by summary
    archived_count: int = 0         # number of turns dropped from conversation

    def total_turns_seen(self) -> int:
        """Absolute turn count, including ones already trimmed."""
        return self.archived_count + len(self.conversation)
