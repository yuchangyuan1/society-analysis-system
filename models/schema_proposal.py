"""
SchemaProposal - redesign-2026-05 Phase 2.

Pydantic contract emitted by `agents/schema_agent.py`. Each ColumnSpec
becomes one row in `schema_meta` AND one document in Chroma 2 (kind=schema).
The Schema-aware Agent never issues ALTER TABLE: dynamic fields all live in
the JSONB `extra` column.

Fingerprinting (PROJECT_REDESIGN_V2.md Phase 2 double-write contract):
    schema_fingerprint = sha256(sorted("table.column.type" for each ColumnSpec))
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


ColumnLocation = Literal["core", "extra"]


class ColumnSpec(BaseModel):
    """One column known to the system."""

    table_name: str
    column_name: str
    column_type: str           # "TEXT" | "INTEGER" | "JSONB" | ...
    description: str           # plain-English meaning, fed to NL2SQL via Chroma 2
    sample_values: list[str] = Field(default_factory=list,
                                     description="up to 5 non-null examples")
    location: ColumnLocation = "extra"

    @property
    def fingerprint_key(self) -> str:
        return f"{self.table_name}.{self.column_name}.{self.column_type}"

    def fingerprint(self) -> str:
        """Per-column sha256; useful for staging-swap diffing."""
        return hashlib.sha256(self.fingerprint_key.encode("utf-8")).hexdigest()


class SchemaProposal(BaseModel):
    """Aggregate output of one Schema-aware Agent run."""

    run_id: str
    proposed_at: datetime = Field(default_factory=datetime.utcnow)
    columns: list[ColumnSpec] = Field(default_factory=list)
    notes: Optional[str] = None  # free-form summary; logged but not persisted

    def schema_fingerprint(self) -> str:
        """Aggregate sha256 across all columns. Compared to Chroma 2's stored
        fingerprint by `tests/test_schema_consistency.py`."""
        keys = sorted(c.fingerprint_key for c in self.columns)
        return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()

    def core_columns(self) -> list[ColumnSpec]:
        return [c for c in self.columns if c.location == "core"]

    def extra_columns(self) -> list[ColumnSpec]:
        return [c for c in self.columns if c.location == "extra"]
