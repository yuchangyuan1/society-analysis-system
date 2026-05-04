"""
Reflection records - redesign-2026-05 Phase 5 (partial; reused in Phase 3).

Phase 3 lands the basic data shape and the auto-removal hook (PROJECT_REDESIGN_V2.md
7b-(5)) so Chroma 2 / Chroma 3 don't accumulate poison records before
Reflection's full looking glass arrives in Phase 5.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


ErrorKind = Literal[
    # Layer 1: NL2SQL internal (NOT routed to Reflection)
    "sql_syntax",
    "sql_unknown_column",
    "sql_type_mismatch",
    "sql_timeout",
    "sql_connection",
    "sql_other",
    # Layer 2: Critic-visible
    "sql_empty_result",
    "sql_limit_hit",
    "missing_branch",
    "wrong_branch_combo",
    "citation_missing",
    "numeric_mismatch",
    "off_topic",
]


FailedBranch = Literal["nl2sql", "kg", "evidence", "planner", "writer"]


class CriticVerdict(BaseModel):
    passed: bool
    error_kind: Optional[ErrorKind] = None
    failed_branch: Optional[FailedBranch] = None
    causal_record_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class ReflectionRecord(BaseModel):
    occurred_at: datetime = Field(default_factory=_utcnow)
    session_id: Optional[str] = None
    user_message: str = ""
    error_kind: Optional[ErrorKind] = None
    failed_branch: Optional[FailedBranch] = None
    causal_record_ids: list[str] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)
