"""
EntitySpan — redesign-2026-05 Phase 1.4.

Replaces v1 NamedEntity (which was bound to Claim). v2 attaches entities
directly to Post; char offsets let Phase 2 write Kuzu MENTIONS edges with
positional fidelity.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


EntityType = Literal["PERSON", "ORG", "LOC", "EVENT", "OTHER"]


class EntitySpan(BaseModel):
    """A single occurrence of an entity in a post's text."""

    name: str = Field(..., description="canonical name")
    entity_type: EntityType = "OTHER"
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)

    def __hash__(self) -> int:
        return hash((self.name.lower(), self.entity_type))
