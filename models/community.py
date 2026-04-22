"""
Phase 1 data models — social network / community analysis layer.

Corresponds to:
  Task 2.7  — Community detection & echo-chamber analysis
  Task 1.2  — Trust and social capital scoring
  Task 2.8  — Cross-platform / cross-community coordination
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class EchoChamberScore(BaseModel):
    """Encapsulates isolation metrics for a single detected community."""
    community_id: str
    label: str
    size: int = 0
    # 0.0 = fully open, 1.0 = completely self-contained
    isolation_score: float = 0.0
    # Topics the community converges on
    dominant_topics: list[str] = Field(default_factory=list)
    # Dominant emotion across community's posts
    dominant_emotion: str = "neutral"
    # Accounts bridging this community to others
    bridge_accounts: list[str] = Field(default_factory=list)
    is_echo_chamber: bool = False  # isolation_score > 0.8


class CommunityInfo(BaseModel):
    """Full community profile including member accounts."""
    community_id: str
    label: str
    size: int = 0
    isolation_score: float = 0.0
    dominant_topics: list[str] = Field(default_factory=list)
    dominant_emotion: str = "neutral"
    is_echo_chamber: bool = False
    account_ids: list[str] = Field(default_factory=list)
    bridge_accounts: list[str] = Field(default_factory=list)


class CoordinationSignal(BaseModel):
    """
    Cross-community coordination signal — different communities posting
    semantically similar content within a narrow time window.
    """
    account_a: str
    community_a: str
    account_b: str
    community_b: str
    similarity_score: float = 0.0
    shared_claim_count: int = 0
    sample_claims: list[str] = Field(default_factory=list)


class CommunityAnalysis(BaseModel):
    """Top-level community analysis result stored in IncidentReport."""
    community_count: int = 0
    echo_chamber_count: int = 0
    communities: list[CommunityInfo] = Field(default_factory=list)
    cross_community_signals: list[CoordinationSignal] = Field(default_factory=list)
    # Modularity Q value — quality of the community partition (> 0.3 is good)
    modularity: Optional[float] = None
    skipped: bool = False   # True when < 20 accounts (insufficient data)
    skip_reason: Optional[str] = None
