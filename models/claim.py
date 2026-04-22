from __future__ import annotations
from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field


class ClaimEvidence(BaseModel):
    article_id: str
    article_title: Optional[str] = None
    article_url: Optional[str] = None
    source_name: Optional[str] = None   # media outlet, e.g. "Reuters", "BBC"
    stance: Literal["supports", "contradicts", "neutral"]
    snippet: Optional[str] = None
    # P0-3: Source tier — indicates which retrieval layer produced this evidence.
    #   internal_chroma: from the pre-populated Chroma articles collection (NewsSearchService)
    #   wikipedia:       from the Wikipedia REST fallback
    #   news:            from a direct NewsSearch call triggered by the claim itself
    source_tier: Literal["internal_chroma", "wikipedia", "news"] = "internal_chroma"


DeduplicationResult = Literal["SAME", "RELATED", "DIFFERENT"]

# P0-1 §10.2 — only these two actionability fields live on Claim. Decision
# metadata (visual_type, explanation, recommended next step) lives on
# IncidentReport.intervention_decision, scoped to the primary claim.
ClaimActionability = Literal["actionable", "non_actionable"]
NonActionableReason = Literal[
    "context_sparse",
    "insufficient_evidence",
    "non_factual_expression",
]


class Claim(BaseModel):
    id: str
    normalized_text: str
    first_seen_post: Optional[str] = None
    propagation_count: int = 1
    risk_level: Optional[str] = None
    supporting_evidence: list[ClaimEvidence] = Field(default_factory=list)
    contradicting_evidence: list[ClaimEvidence] = Field(default_factory=list)
    uncertain_evidence: list[ClaimEvidence] = Field(default_factory=list)
    related_claim_ids: list[str] = Field(default_factory=list)
    same_as_claim_ids: list[str] = Field(default_factory=list)
    # P0-1: two-class actionability + reason code (rule-based, no LLM).
    claim_actionability: Optional[ClaimActionability] = None
    non_actionable_reason: Optional[NonActionableReason] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def evidence_summary(self) -> dict:
        return {
            "supporting": len(self.supporting_evidence),
            "contradicting": len(self.contradicting_evidence),
            "uncertain": len(self.uncertain_evidence),
        }

    def has_sufficient_evidence(self, min_items: int = 1) -> bool:
        total = (
            len(self.supporting_evidence)
            + len(self.contradicting_evidence)
            + len(self.uncertain_evidence)
        )
        return total >= min_items

    def has_actionable_counter_evidence(self) -> bool:
        # True iff we hold something concrete to rebut with. Uncertain-only
        # evidence produces vague "can't confirm or deny" counter-messages
        # that waste an SD generation without substantive rebuttal; callers
        # should skip counter-messaging in that case.
        return len(self.contradicting_evidence) >= 1
