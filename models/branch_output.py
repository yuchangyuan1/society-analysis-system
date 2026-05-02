"""
Branch outputs - redesign-2026-05 Phase 3.

The three retrieval branches each emit a typed output that the Planner
collects and the Report Writer composes from. Keeping them separate (vs. a
single union) keeps every branch's responsibilities crisp and gives the
Critic agent (Phase 4) deterministic shape checks.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from models.evidence import EvidenceBundle


# ── A. Evidence Retrieval ────────────────────────────────────────────────────

class EvidenceOutput(BaseModel):
    branch: Literal["evidence"] = "evidence"
    bundle: EvidenceBundle


# ── B. NL2SQL ────────────────────────────────────────────────────────────────

class SQLAttempt(BaseModel):
    """One iteration inside the NL2SQL repair loop."""

    sql: str
    error: Optional[str] = None
    error_kind: Optional[str] = None  # sql_syntax / sql_unknown_column / ...
    rows_returned: int = 0


class SQLOutput(BaseModel):
    branch: Literal["nl2sql"] = "nl2sql"
    nl_query: str
    final_sql: str = ""
    rows: list[dict] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    attempts: list[SQLAttempt] = Field(default_factory=list)
    used_examples: list[str] = Field(default_factory=list)
    used_error_lessons: list[str] = Field(default_factory=list)
    used_schema_hints: list[str] = Field(default_factory=list)
    success: bool = False
    elapsed_ms: int = 0


# ── C. Knowledge Graph Query ─────────────────────────────────────────────────

KGQueryKind = Literal[
    "propagation_path",
    "key_nodes",
    "community_relations",
    "topic_correlation",
]


class KGNode(BaseModel):
    id: str
    label: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class KGEdge(BaseModel):
    source_id: str
    target_id: str
    rel_type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class KGOutput(BaseModel):
    branch: Literal["kg"] = "kg"
    query_kind: KGQueryKind
    target: dict[str, Any] = Field(default_factory=dict)  # topic_id / account_id / ...
    nodes: list[KGNode] = Field(default_factory=list)
    edges: list[KGEdge] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0


# ── Aggregation envelope used by the Planner ────────────────────────────────

class BranchExecutionStatus(BaseModel):
    branch: Literal["evidence", "nl2sql", "kg"]
    success: bool
    error: Optional[str] = None
    error_kind: Optional[str] = None
    elapsed_ms: int = 0
