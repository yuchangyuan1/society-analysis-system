"""
Risk Workspace — misinfo-risk-review skill.

Responsibilities:
  - Score misinformation likelihood for a claim
  - Flag suspicious propagation patterns
  - Route high-risk outputs to human review queue
"""
from __future__ import annotations

import json
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.claim import Claim
from models.risk_assessment import RiskAssessment, RiskLevel
from models.report import PropagationSummary

log = structlog.get_logger(__name__)

_RISK_SYSTEM = """You are a misinformation risk analyst for a social media intelligence platform.
Given a claim and its evidence, produce a structured risk assessment.

Return JSON with:
  misinfo_score: float 0.0-1.0 (1.0 = high likelihood of misinformation)
  risk_level: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
  reasoning: string (max 150 words)
  flags: list[string] — triggered warning flags
  requires_human_review: bool

Flags to consider:
  - "no_supporting_evidence": claim has no supporting evidence
  - "strong_contradiction": multiple high-quality fact-checks contradict the claim
  - "propagation_anomaly": suspicious velocity or coordinated spreading
  - "insufficient_evidence": fewer than 2 total evidence items

Return ONLY the JSON object."""


class RiskAgent:
    def __init__(self) -> None:
        self._claude = openai.OpenAI(api_key=OPENAI_API_KEY)

    # ── Skill: misinfo-risk-review ────────────────────────────────────────────

    def assess_risk(
        self,
        claim: Claim,
        propagation: Optional[PropagationSummary] = None,
    ) -> RiskAssessment:
        """
        Evaluate misinformation risk for a claim.
        Returns RiskAssessment; sets requires_human_review if warranted.
        """
        # Block: insufficient evidence
        if not claim.has_sufficient_evidence(min_items=1):
            log.warning(
                "risk.insufficient_evidence",
                claim_id=claim.id,
                text=claim.normalized_text[:80],
            )
            return RiskAssessment(
                claim_id=claim.id,
                risk_level=RiskLevel.INSUFFICIENT_EVIDENCE,
                misinfo_score=0.5,
                reasoning="No evidence retrieved. Cannot assess risk.",
                flags=["insufficient_evidence"],
                requires_human_review=True,
            )

        context = self._build_context(claim, propagation)
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": _RISK_SYSTEM},
                    {"role": "user", "content": context},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            data = json.loads(raw) if raw else {}
            assessment = RiskAssessment(
                claim_id=claim.id,
                risk_level=RiskLevel(data.get("risk_level", "MEDIUM")),
                misinfo_score=float(data.get("misinfo_score", 0.5)),
                reasoning=data.get("reasoning", ""),
                flags=data.get("flags", []),
                requires_human_review=bool(data.get("requires_human_review", False)),
                propagation_anomaly=propagation.anomaly_detected
                if propagation else False,
            )
            if propagation and propagation.anomaly_detected:
                assessment.flags.append("propagation_anomaly")
                if assessment.risk_level not in (RiskLevel.HIGH, RiskLevel.CRITICAL):
                    assessment.risk_level = RiskLevel.HIGH
                assessment.requires_human_review = True
            log.info(
                "risk.assessment",
                claim_id=claim.id,
                risk_level=assessment.risk_level,
                score=assessment.misinfo_score,
            )
            return assessment
        except Exception as exc:
            log.error("risk.llm_error", error=str(exc))
            return RiskAssessment(
                claim_id=claim.id,
                risk_level=RiskLevel.MEDIUM,
                misinfo_score=0.5,
                reasoning=f"Risk assessment failed: {exc}",
                flags=["assessment_error"],
                requires_human_review=True,
            )

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_context(claim: Claim,
                       propagation: Optional[PropagationSummary]) -> str:
        parts = [f"Claim: {claim.normalized_text}"]
        ev = claim.evidence_summary()
        parts.append(
            f"Evidence: {ev['supporting']} supporting, "
            f"{ev['contradicting']} contradicting, "
            f"{ev['uncertain']} uncertain"
        )
        if claim.contradicting_evidence:
            snippets = "; ".join(
                e.snippet or e.article_title or ""
                for e in claim.contradicting_evidence[:3]
            )
            parts.append(f"Contradictions: {snippets}")
        if claim.supporting_evidence:
            snippets = "; ".join(
                e.snippet or e.article_title or ""
                for e in claim.supporting_evidence[:3]
            )
            parts.append(f"Support: {snippets}")
        if propagation:
            parts.append(
                f"Propagation: {propagation.post_count} posts, "
                f"velocity={propagation.velocity:.1f}/hr, "
                f"anomaly={propagation.anomaly_detected}"
            )
        return "\n".join(parts)
