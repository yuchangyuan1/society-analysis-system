"""
Phase 3 — Propagation immunity / inoculation strategy models.

Corresponds to:
  Task 1.8  — Propagation immunity strategy (graph-based vaccination targeting)

Design:
  The system selects a minimal set of "vaccination nodes" (accounts) whose
  targeted inoculation (prebunking, direct counter-messaging, etc.) would
  maximally reduce the spread of a mis-information claim across the network.

  Selection is based on a combination of:
    - Betweenness centrality   (bridge nodes — control inter-community flow)
    - PageRank / influence     (amplifier nodes — high reach within community)
    - Echo-chamber periphery   (entry-point nodes — new community gateways)

  immunity_coverage: estimated fraction of the network "protected" if all
  recommended targets receive the inoculation message.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ImmunizationTarget(BaseModel):
    """A single account recommended for inoculation."""
    account_id: str
    account_name: str = ""
    role: str = "PASSIVE"                   # ORIGINATOR | AMPLIFIER | BRIDGE | PASSIVE
    community_id: Optional[str] = None
    community_label: str = ""

    betweenness_centrality: float = 0.0     # 0–1; higher = more bridge-like
    pagerank_score: float = 0.0             # 0–1; higher = more influential
    estimated_reach: int = 0                # posts-per-day this account typically produces
    is_echo_chamber_entry: bool = False     # sits at community boundary

    priority_score: float = 0.0            # composite; used for ranking
    inoculation_message: str = ""           # tailored pre-bunk text for this target
    rationale: str = ""                     # plain-English reason for selection


class ImmunityStrategy(BaseModel):
    """
    Full inoculation strategy for one incident / topic cluster.

    immunity_coverage is a heuristic estimate:
      coverage ≈ 1 − (1 − avg_pagerank)^n_targets
    clamped to [0, 1].
    """
    topic_id: Optional[str] = None
    topic_label: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)

    targets: list[ImmunizationTarget] = Field(default_factory=list)
    total_accounts_analyzed: int = 0
    recommended_target_count: int = 0

    # Estimated network coverage if all targets are reached
    immunity_coverage: float = 0.0          # 0.0–1.0

    # Which selection strategy was used
    strategy_used: str = "betweenness+pagerank"

    # Plain-English summary for the report
    summary: str = ""
    skipped: bool = False
    skip_reason: str = ""
