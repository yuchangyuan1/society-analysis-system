"""
Phase 2 data models — predictive and intervention layer.

Covers:
  Task 1.10 — Cascade prediction (24h spread forecast)
  Task 1.4  — Meme persuasion feature analysis
  Task 3.3  — Optimal counter-messaging target selection
  Task 1.3  — Named entity co-occurrence (entity relationship network)
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


# ── Task 1.10: Cascade Prediction ─────────────────────────────────────────────

class CascadePrediction(BaseModel):
    """
    24-hour propagation forecast for a given topic or claim.
    Uses early-signal features: velocity, emotion weight, influencer score,
    community isolation, bridge account count.
    """
    topic_id: Optional[str] = None
    topic_label: Optional[str] = None

    # Input signals used for prediction
    current_velocity: float = 0.0          # posts/hr at prediction time
    emotion_weight: float = 0.0            # fear/anger → higher weight
    top_influencer_score: float = 0.0      # max PageRank in originator accounts
    community_isolation: float = 0.0       # avg isolation score of detected communities
    bridge_account_count: int = 0          # cross-community bridge accounts

    # Forecast outputs
    predicted_posts_24h: int = 0           # projected total post count in 24h
    predicted_new_communities: int = 0     # number of new communities likely to adopt
    peak_window_hours: Optional[str] = None  # e.g. "2-6h" — estimated peak engagement
    confidence: str = "LOW"               # LOW | MEDIUM | HIGH
    reasoning: str = ""


# ── Task 1.4: Meme Persuasion Analysis ────────────────────────────────────────

class PersuasionFeatures(BaseModel):
    """
    Quantified persuasion dimensions of a claim or post.
    Based on Task 1.4 — Meme/campaign persuasion decomposition.
    """
    claim_id: Optional[str] = None
    claim_text: Optional[str] = None

    # Continuous scores (0.0–1.0)
    emotional_appeal: float = 0.0      # overall emotional charge strength
    fear_framing: float = 0.0          # specifically fear-laden framing
    simplicity_score: float = 0.0      # simpler = more shareable (higher = simpler)

    # Discrete signals
    authority_reference: bool = False   # references expert / official source
    urgency_markers: int = 0            # count of "BREAKING", "CONFIRMED", "NOW", etc.
    identity_trigger: bool = False      # activates in-group / out-group identity

    # Composite virality risk (computed internally)
    virality_score: float = 0.0        # 0.0–1.0 composite
    top_persuasion_tactic: str = ""     # "fear_framing" | "simplicity" | "authority" | …
    explanation: str = ""


# ── Task 3.3: Counter-Messaging Target Recommendation ─────────────────────────

class CounterTargetRec(BaseModel):
    """
    Recommended account / audience segment for counter-message delivery.
    Prioritised by expected persuasion cost-effectiveness.
    """
    account_id: str
    username: str = ""
    role: str = ""                         # BRIDGE | AMPLIFIER | PASSIVE | ORIGINATOR
    community_id: Optional[str] = None
    trust_score: float = 0.0              # 0.0–1.0
    influence_score: float = 0.0          # PageRank value
    priority_rank: int = 0                # 1 = highest priority
    rationale: str = ""                   # why this account is a good target


class CounterTargetPlan(BaseModel):
    """Full counter-targeting plan for a given claim or campaign."""
    claim_id: Optional[str] = None
    recommended_targets: list[CounterTargetRec] = Field(default_factory=list)
    excluded_accounts: list[str] = Field(default_factory=list)  # ORIGINATORs excluded
    strategy_summary: str = ""


# ── Task 1.3 supplement: Named Entity Network ─────────────────────────────────

class NamedEntity(BaseModel):
    """A named entity extracted from claims."""
    entity_id: str
    name: str
    entity_type: str = "UNKNOWN"   # PERSON | ORG | PLACE | EVENT | UNKNOWN
    mention_count: int = 0


class EntityCoOccurrence(BaseModel):
    """Two entities that frequently co-occur in claims."""
    entity_a_id: str
    entity_a_name: str
    entity_b_id: str
    entity_b_name: str
    co_occurrence_count: int = 0
    shared_claim_ids: list[str] = Field(default_factory=list)
