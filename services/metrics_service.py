"""
MetricsService — per-run quantitative metrics (P0-5).

Writes data/runs/{run_id}/metrics.json with:
  - evidence_coverage:               fraction of claims that have ≥1 evidence
  - evidence_tier_distribution:      {internal_chroma, wikipedia, news} counts
  - community_modularity_q:          Louvain Q value, or None if skipped
  - account_role_counts:             {ORIGINATOR, AMPLIFIER, BRIDGE, PASSIVE, ...}
  - counter_effect_closed_loop_rate: (total - pending) / total across all history
  - run_id, computed_at

The MetricsService never mutates report state — it only reads from the
structured IncidentReport + CounterEffectService.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from models.claim import Claim
from models.report import IncidentReport
from services.counter_effect_service import CounterEffectService

log = structlog.get_logger(__name__)


class MetricsService:
    def compute(
        self,
        report: IncidentReport,
        claims: list[Claim],
        counter_effect_service: Optional[CounterEffectService] = None,
        run_id: Optional[str] = None,
        role_risk_correlation: Optional[float] = None,
    ) -> dict:
        # evidence_coverage
        total_claims = len(claims)
        with_ev = sum(1 for c in claims if _claim_has_any_evidence(c))
        evidence_coverage = (with_ev / total_claims) if total_claims > 0 else 0.0

        # tier distribution
        tier_dist: dict[str, int] = {"internal_chroma": 0, "wikipedia": 0, "news": 0}
        for c in claims:
            for pool in (c.supporting_evidence, c.contradicting_evidence, c.uncertain_evidence):
                for e in pool:
                    tier = getattr(e, "source_tier", "internal_chroma") or "internal_chroma"
                    tier_dist[tier] = tier_dist.get(tier, 0) + 1

        # modularity
        modularity_q: Optional[float] = None
        if report.community_analysis and not report.community_analysis.skipped:
            modularity_q = report.community_analysis.modularity

        # role counts
        role_counts: dict[str, int] = {}
        if report.propagation_summary:
            role_counts = dict(report.propagation_summary.account_role_summary)

        # counter_effect closed loop rate
        closed_loop_rate: Optional[float] = None
        ce_summary: dict = {}
        if counter_effect_service is not None:
            try:
                ce_report = counter_effect_service.get_effect_report()
                total = ce_report.total_tracked
                closed = total - ce_report.pending_followup
                closed_loop_rate = (closed / total) if total > 0 else 0.0
                ce_summary = {
                    "total_tracked": total,
                    "pending_followup": ce_report.pending_followup,
                    "effective_count": ce_report.effective_count,
                    "neutral_count": ce_report.neutral_count,
                    "backfired_count": ce_report.backfired_count,
                    "average_effect_score": ce_report.average_effect_score,
                }
            except Exception as exc:
                log.warning("metrics.counter_effect_error", error=str(exc))

        # P0-1: actionability distribution (also written to report_raw.json
        # via claim fields; here we aggregate a fast summary for metrics.json).
        actionability_dist = {
            "actionable": 0,
            "non_actionable": 0,
            "context_sparse": 0,
            "insufficient_evidence": 0,
            "non_factual_expression": 0,
        }
        for c in claims:
            if c.claim_actionability:
                actionability_dist[c.claim_actionability] = (
                    actionability_dist.get(c.claim_actionability, 0) + 1
                )
            if c.non_actionable_reason:
                actionability_dist[c.non_actionable_reason] = (
                    actionability_dist.get(c.non_actionable_reason, 0) + 1
                )

        # P0-2: intervention decision summary
        intervention = report.intervention_decision
        intervention_summary: Optional[dict] = None
        if intervention is not None:
            intervention_summary = {
                "decision": intervention.decision,
                "reason": intervention.reason,
                "recommended_next_step": intervention.recommended_next_step,
                "visual_type": intervention.visual_type,
            }

        # P0-4: bridge_influence_ratio (always present — 0.0 when no bridges)
        bridge_ratio = 0.0
        if report.propagation_summary is not None:
            bridge_ratio = report.propagation_summary.bridge_influence_ratio

        metrics = {
            "run_id": run_id,
            "computed_at": datetime.utcnow().isoformat(),
            "evidence_coverage": round(evidence_coverage, 4),
            "evidence_with_any": with_ev,
            "evidence_total_claims": total_claims,
            "evidence_tier_distribution": tier_dist,
            "community_modularity_q": modularity_q,
            "account_role_counts": role_counts,
            "counter_effect_closed_loop_rate": (
                round(closed_loop_rate, 4) if closed_loop_rate is not None else None
            ),
            "counter_effect_summary": ce_summary,
            "counter_message_deployed": bool(report.counter_message),
            "counter_message_skip_reason": report.counter_message_skip_reason,
            "actionable_counter_evidence_rate": round(
                _actionable_rate(claims), 4
            ),
            # P0-1 / P0-2 / P0-4
            "actionability_distribution": actionability_dist,
            "intervention_decision": intervention_summary,
            "bridge_influence_ratio": round(bridge_ratio, 4),
            "role_risk_correlation": (
                round(role_risk_correlation, 4)
                if role_risk_correlation is not None else None
            ),
            "risk_level": report.risk_level,
            "post_count": report.post_count,
            "topic_count": len(report.topic_summaries or []),
        }
        return metrics

    def write(self, run_dir: Path, metrics: dict) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "metrics.json"
        path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        log.info("metrics.written", path=str(path))
        return path


def _claim_has_any_evidence(claim: Claim) -> bool:
    return bool(
        claim.supporting_evidence
        or claim.contradicting_evidence
        or claim.uncertain_evidence
    )


def _actionable_rate(claims: list[Claim]) -> float:
    # Share of claims that have ≥1 contradicting evidence item — the signal
    # the counter-message gate uses. A low value with high evidence_coverage
    # means the stance classifier routed everything to "uncertain".
    if not claims:
        return 0.0
    actionable = sum(1 for c in claims if c.contradicting_evidence)
    return actionable / len(claims)
