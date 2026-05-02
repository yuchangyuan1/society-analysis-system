"""JSON-file session store for the chat orchestrator.

One session per file at `data/sessions/{session_id}.json`. Single user,
single machine — no concurrency locks. Callers MUST serialize their own
access if they ever run multiple requests concurrently.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from models.session import SessionState, ConversationTurn


_SESSIONS_DIR = Path(config.DATA_DIR) / "sessions"


def _path(session_id: str) -> Path:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    # Basic sanitization — session_id should never contain path separators.
    safe = session_id.replace("/", "_").replace("\\", "_")
    return _SESSIONS_DIR / f"{safe}.json"


def load(session_id: str) -> SessionState:
    """Load existing session or create an empty one."""
    p = _path(session_id)
    if p.exists():
        try:
            return SessionState.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt file — start fresh (but don't delete original).
            pass
    return SessionState(session_id=session_id)


def save(state: SessionState) -> None:
    """Write the session atomically (write temp + rename)."""
    p = _path(state.session_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        state.model_dump_json(indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


def append_turn(
    state: SessionState,
    role: str,
    content: str,
    capability_used: Optional[str] = None,
    branches_used: Optional[list[str]] = None,
) -> SessionState:
    """Append a conversation turn in-place and return the state."""
    state.conversation.append(
        ConversationTurn(
            role=role,
            content=content,
            capability_used=capability_used,
            branches_used=list(branches_used or []),
            at=datetime.utcnow(),
        )
    )
    return state
