"""
ModuleCard - redesign-2026-05 Phase 3.5.

Each retrieval branch ships one `ModuleCard` describing what it does, when
to use it, and the input/output shapes. The Planner reads these from
Chroma 3 (kind=module_card) when picking branches; Phase 3 ships the
seed cards as code constants.

PROJECT_REDESIGN_V2.md 7b-(4):
    - Don't hard-code Planner prompt with branch knowledge
    - Each branch self-describes via ModuleCard
    - Planner queries Chroma 3 for relevant cards on demand
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


BranchName = Literal["evidence", "nl2sql", "kg"]


class ModuleCard(BaseModel):
    name: BranchName
    description: str
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    examples: list[dict[str, Any]] = Field(default_factory=list)

    def doc_text(self) -> str:
        """Stringification used for Chroma 3 embedding."""
        parts = [
            f"Branch: {self.name}",
            f"Description: {self.description}",
            "When to use:",
            *(f"  - {x}" for x in self.when_to_use),
        ]
        if self.when_not_to_use:
            parts.append("When NOT to use:")
            parts.extend(f"  - {x}" for x in self.when_not_to_use)
        if self.examples:
            parts.append("Examples:")
            for i, ex in enumerate(self.examples, start=1):
                q = ex.get("question") or ex.get("input") or ""
                parts.append(f"  {i}. {q}")
        return "\n".join(parts)


class WorkflowExemplar(BaseModel):
    """One question -> branch combination exemplar (kind=workflow_success)."""

    question: str
    branches_used: list[BranchName]
    rationale: str = ""

    def doc_text(self) -> str:
        return (
            f"Question: {self.question}\n"
            f"Branches: {', '.join(self.branches_used)}\n"
            f"Rationale: {self.rationale}"
        )
