"""
Evidence contracts - redesign-2026-05 Phase 3.

Data contracts emitted by the Evidence Retrieval branch (`tools/hybrid_retrieval.py`).
Consumed by the Report Writer agent (Phase 4) to produce citation-bearing
answers.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


SourceTier = Literal["official", "reputable_media", "user_generated", "unknown"]


class Citation(BaseModel):
    """User-visible citation for one evidence chunk."""

    chunk_id: str
    source: str             # short site name, e.g. "bbc"
    domain: str             # canonical domain, e.g. "bbc.com"
    tier: SourceTier = "reputable_media"
    title: str = ""
    url: str = ""
    publish_date: Optional[datetime] = None


class EvidenceChunk(BaseModel):
    """One retrieved chunk plus its retrieval scores."""

    chunk_id: str
    text: str
    citation: Citation
    dense_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    rrf_score: float = 0.0
    rerank_score: Optional[float] = None
    final_rank: int = 0


class EvidenceBundle(BaseModel):
    """Final result of one Evidence Retrieval invocation."""

    query: str
    chunks: list[EvidenceChunk] = Field(default_factory=list)
    rerank_used: bool = False
    metadata_filter: dict = Field(default_factory=dict)
    elapsed_ms: int = 0
    notes: list[str] = Field(default_factory=list)
