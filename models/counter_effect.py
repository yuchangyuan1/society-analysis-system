"""
Phase 3 — Counter-effect tracking data models.

Corresponds to:
  Task 3.2  — Counter-campaign deployment & effectiveness evaluation
  Task 3.7  — Competitive meme intervention monitoring

Design:
  When a counter-message is deployed (TREND_ANALYSIS run), the system saves
  a baseline snapshot (velocity, post_count) tied to that report.
  On a subsequent run covering the same topic, it records a follow-up snapshot
  and computes an effect_score indicating how much the counter-message reduced
  (or failed to reduce) propagation velocity.

  effect_score > 0   → propagation slowed (positive intervention outcome)
  effect_score ≈ 0   → no measurable effect
  effect_score < 0   → propagation accelerated (backfire / topic gained attention)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class CounterEffectRecord(BaseModel):
    """Single deployment record tying a counter-message to its velocity outcome."""
    record_id: str
    report_id: str                          # source IncidentReport.id
    claim_id: Optional[str] = None
    topic_id: Optional[str] = None
    topic_label: Optional[str] = None

    # Counter-message metadata
    counter_message: str = ""
    deployed_at: datetime = Field(default_factory=datetime.utcnow)

    # Baseline (at deployment time)
    baseline_velocity: float = 0.0          # posts/hr when counter was deployed
    baseline_post_count: int = 0

    # Follow-up (measured on next run)
    followup_velocity: Optional[float] = None
    followup_post_count: Optional[int] = None
    followup_at: Optional[datetime] = None

    # Derived metrics (computed after follow-up is available)
    velocity_delta: Optional[float] = None  # followup - baseline (negative = good)
    decay_rate: Optional[float] = None      # (baseline - followup) / baseline
    effect_score: Optional[float] = None    # normalised to -1..+1
    outcome: Optional[str] = "PENDING"      # EFFECTIVE | NEUTRAL | BACKFIRED | PENDING


class CounterEffectReport(BaseModel):
    """Aggregated effectiveness report across all tracked deployments."""
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)
    total_tracked: int = 0
    pending_followup: int = 0               # records without follow-up yet
    effective_count: int = 0               # effect_score > 0.2
    neutral_count: int = 0
    backfired_count: int = 0

    average_effect_score: Optional[float] = None
    average_decay_rate: Optional[float] = None

    best_performing: list[CounterEffectRecord] = Field(default_factory=list)
    worst_performing: list[CounterEffectRecord] = Field(default_factory=list)

    # Key insight for the report
    summary: str = ""
