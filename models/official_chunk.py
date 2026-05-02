"""
OfficialChunk — redesign-2026-05 Phase 1.5.

Chunk unit for the official-source ingestion arm. Phase 1 writes jsonl;
Phase 2 upserts to Chroma 1 (articles collection).

Fields align with the Chroma 1 metadata contract in PROJECT_REDESIGN_V2.md
section 5b: source / publish_date / tier / topic (topic_hint is optional
and filled by a downstream matcher).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


SourceTier = Literal["official", "reputable_media", "user_generated", "unknown"]


class OfficialChunk(BaseModel):
    chunk_id: str = Field(..., description="sha256 of (url + chunk_index)")
    source: str  # site short name, e.g. "bbc"
    domain: str  # canonical domain, e.g. "bbc.com"
    tier: SourceTier = "reputable_media"
    url: str
    title: str = ""
    author: str = ""
    publish_date: Optional[datetime] = None
    chunk_index: int = 0
    text: str = ""
    token_count: int = 0
    topic_hint: Optional[str] = None

    # Phase 2 sets this when the chunk is upserted into Chroma 1
    embedded: bool = False
