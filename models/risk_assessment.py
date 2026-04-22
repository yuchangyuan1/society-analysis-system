from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RiskAssessment(BaseModel):
    claim_id: str
    risk_level: RiskLevel
    misinfo_score: float = Field(ge=0.0, le=1.0)
    reasoning: str
    flags: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
    propagation_anomaly: bool = False

    def is_blocked(self) -> bool:
        return (
            self.risk_level == RiskLevel.INSUFFICIENT_EVIDENCE
            or self.requires_human_review
        )
