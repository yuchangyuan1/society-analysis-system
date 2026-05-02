"""
ReportV2 - redesign-2026-05 Phase 4.3.

Final user-facing artefact emitted by `agents/report_writer.py`. Replaces
v1's `IncidentReport` for the chat path (the v1 model stays alive for the
v1 pipeline). The Quality Critic operates on this object before it leaves
the system.

The shape is intentionally narrow: a markdown body, a deduplicated
citation list, and a numbers_table that lets the Critic check numerical
claims against SQL output without re-parsing prose.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from models.evidence import Citation


class ReportNumber(BaseModel):
    """One numerical fact lifted from the source rows.

    The Writer is required to populate this whenever it cites a number in
    the markdown body. The Critic compares each row to its source.
    """

    label: str
    value: float
    source_branch: str          # "nl2sql" | "kg" | "evidence"
    source_ref: Optional[str] = None  # SQL fingerprint / KG metric key / chunk_id


class ReportV2(BaseModel):
    user_question: str
    markdown_body: str = ""
    citations: list[Citation] = Field(default_factory=list)
    numbers: list[ReportNumber] = Field(default_factory=list)
    branches_used: list[str] = Field(default_factory=list)
    needs_human_review: bool = False
    notes: list[str] = Field(default_factory=list)

    def has_citations(self) -> bool:
        return len(self.citations) > 0
