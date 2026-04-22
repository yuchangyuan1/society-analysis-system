from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

# Phase 1 + 2 models — imported directly to allow Pydantic to resolve type annotations
from models.claim import Claim
from models.community import CommunityAnalysis
from models.persuasion import (
    CascadePrediction, PersuasionFeatures,
    CounterTargetPlan, NamedEntity, EntityCoOccurrence,
)
from models.immunity import ImmunityStrategy
from models.counter_effect import CounterEffectRecord


# P0-2 §10.2 — intervention decision lives on the report, scoped to the
# primary claim. Visual_type + decision_summary belong here, NOT on Claim.
class InterventionDecision(BaseModel):
    primary_claim_id: Optional[str] = None
    primary_claim_text: Optional[str] = None
    decision: str                     # "rebut" | "evidence_context" | "abstain"
    reason: Optional[str] = None      # actionability reason code, e.g. "context_sparse"
    explanation: str = ""             # human-readable one-liner for the report
    recommended_next_step: str = ""   # "monitor" | "human_review" | "summarize" | "none"
    visual_type: Optional[str] = None # "rebuttal_card" | "evidence_context_card" | None


class StageStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    BLOCKED = "blocked"


class RunLog(BaseModel):
    stage: str
    status: StageStatus
    detail: Optional[str] = None
    logged_at: datetime = Field(default_factory=datetime.utcnow)


class CoordinationPair(BaseModel):
    """Two accounts that independently posted the same claims — coordination signal."""
    account1: str
    account2: str
    shared_claim_count: int
    sample_claims: list[str] = Field(default_factory=list)


class PropagationSummary(BaseModel):
    topic: Optional[str] = None
    post_count: int = 0
    unique_accounts: int = 0
    velocity: float = 0.0           # posts / hour
    stance_distribution: dict[str, int] = Field(default_factory=dict)
    anomaly_detected: bool = False
    anomaly_description: Optional[str] = None
    # Graph-derived coordination signals
    coordinated_pairs: int = 0
    coordination_details: list[CoordinationPair] = Field(default_factory=list)
    # Phase 0: Account Role Classification (Task 2.2)
    account_role_summary: dict[str, int] = Field(default_factory=dict)
    # e.g. {"ORIGINATOR": 3, "AMPLIFIER": 8, "BRIDGE": 2, "PASSIVE": 81}
    # P0-4 §10.5: bridge influence = share of posts authored by BRIDGE-role
    # accounts. 0.0 when no bridges or no posts. Domain: [0.0, 1.0].
    bridge_influence_ratio: float = 0.0


class TopicSummary(BaseModel):
    """Aggregated view of a semantic topic cluster discovered across ingested posts."""
    topic_id: str
    label: str
    claim_count: int = 0
    post_count: int = 0
    velocity: float = 0.0           # posts / hour within this topic
    is_trending: bool = False
    misinfo_risk: float = 0.0       # 0.0–1.0 average across claims
    is_likely_misinfo: bool = False
    representative_claims: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    # Phase 0: Emotional State
    dominant_emotion: str = ""                          # fear | anger | hope | disgust | neutral
    emotion_distribution: dict[str, float] = Field(default_factory=dict)  # {"fear": 0.6, ...}


class IncidentReport(BaseModel):
    id: str
    intent_type: str
    query_text: Optional[str] = None
    claims: list[Claim] = Field(default_factory=list)
    risk_level: Optional[str] = None
    requires_human_review: bool = False
    propagation_summary: Optional[PropagationSummary] = None
    topic_summaries: list[TopicSummary] = Field(default_factory=list)
    counter_message: Optional[str] = None
    counter_message_skip_reason: Optional[str] = None
    visual_card_path: Optional[str] = None
    topic_card_paths: list[str] = Field(default_factory=list)
    report_md: Optional[str] = None
    run_logs: list[RunLog] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Phase 1: Community Analysis (Task 2.7)
    community_analysis: Optional[CommunityAnalysis] = None
    # Phase 2: Predictive & Intervention Layer
    cascade_predictions: list[CascadePrediction] = Field(default_factory=list)
    persuasion_features: list[PersuasionFeatures] = Field(default_factory=list)
    counter_target_plan: Optional[CounterTargetPlan] = None
    top_entities: list[NamedEntity] = Field(default_factory=list)
    entity_co_occurrences: list[EntityCoOccurrence] = Field(default_factory=list)
    # Phase 3: Feedback loop
    immunity_strategy: Optional[ImmunityStrategy] = None
    counter_effect_records: list[CounterEffectRecord] = Field(default_factory=list)
    # P0-2: Intervention decision — explicit, renderable output even when no
    # counter-message is deployed. Populated after primary_claim is selected.
    intervention_decision: Optional[InterventionDecision] = None

    # Reproducibility (filled by planner when run_dir is set — P0-2)
    posts_snapshot_sha256: Optional[str] = None
    post_count: int = 0

    def log(self, stage: str, status: StageStatus, detail: str | None = None) -> None:
        self.run_logs.append(RunLog(stage=stage, status=status, detail=detail))

    def has_flag(self, flag: str) -> bool:
        return any(log.stage == flag for log in self.run_logs)
