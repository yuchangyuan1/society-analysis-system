"""Pydantic models - redesign-2026-05 v2.

The v1 IncidentReport / Claim / RiskAssessment models were deleted in
Phase 5 cleanup. Front-end answers now flow through `models/report_v2.py`
and the per-branch outputs in `models/branch_output.py`.
"""
from .branch_output import (
    BranchExecutionStatus,
    EvidenceOutput,
    KGEdge,
    KGNode,
    KGOutput,
    SQLAttempt,
    SQLOutput,
)
from .chat import ChatMessage, ChatQuery, ChatResponse
from .entity import EntitySpan
from .evidence import Citation, EvidenceBundle, EvidenceChunk
from .module_card import ModuleCard, WorkflowExemplar
from .official_chunk import OfficialChunk
from .post import ImageAsset, Post
from .query import RewrittenQuery, Subtask, SubtaskTarget
from .reflection import CriticVerdict, ReflectionRecord
from .report_v2 import ReportNumber, ReportV2
from .schema_proposal import ColumnSpec, SchemaProposal
from .session import ConversationTurn, SessionState

__all__ = [
    # branch outputs
    "BranchExecutionStatus", "EvidenceOutput", "KGEdge", "KGNode", "KGOutput",
    "SQLAttempt", "SQLOutput",
    # chat
    "ChatMessage", "ChatQuery", "ChatResponse",
    # entities & evidence
    "EntitySpan", "Citation", "EvidenceBundle", "EvidenceChunk",
    "OfficialChunk",
    # planner / writer
    "ModuleCard", "WorkflowExemplar",
    "RewrittenQuery", "Subtask", "SubtaskTarget",
    "ReportNumber", "ReportV2",
    "CriticVerdict", "ReflectionRecord",
    # storage / posts
    "ImageAsset", "Post", "ConversationTurn", "SessionState",
    # schema
    "ColumnSpec", "SchemaProposal",
]
