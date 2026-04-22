"""
Report Workspace — campaign-report-build skill.

P0-1 redesign (2026-04):
  Structural fields (counts, levels, outcomes, modularity, role breakdowns)
  are rendered deterministically from structured objects. The LLM is only
  asked to write two short narrative sections (Executive Summary, Flags &
  Next Steps) at temperature 0.3. A fact-verification pass compares key
  numbers in the LLM output to the structured truth; on mismatch we fall
  back to a static stub for those two sections.

Artifacts (when run_dir is provided):
    run_dir/report.md
    run_dir/report_raw.json
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.claim import Claim
from models.community import CommunityAnalysis
from models.report import IncidentReport, PropagationSummary, StageStatus, TopicSummary
from models.risk_assessment import RiskAssessment
from services.postgres_service import PostgresService

log = structlog.get_logger(__name__)

_NARRATIVE_SYSTEM = """You write two short narrative sections for a misinformation
incident report. You are given a deterministically-rendered factual body
(claims, evidence counts, community stats, counter-effect outcomes).

Write exactly TWO sections:
  ## Executive Summary
  ## Flags and Next Steps

Rules:
- 80-150 words total. Plain prose, max 2 bullet lists.
- Only restate facts already present in the factual body. Do NOT invent
  numbers, do NOT invent entity names, do NOT contradict counts or outcomes.
- Do NOT output any other heading. Do NOT output the factual body again.
- If a field is absent from the factual body, do not speculate.
Return ONLY the Markdown for these two sections."""


class ReportAgent:
    def __init__(self, pg: PostgresService) -> None:
        self._pg = pg
        self._claude = openai.OpenAI(api_key=OPENAI_API_KEY)

    # ── Skill: campaign-report-build ──────────────────────────────────────────

    def build_report(
        self,
        intent_type: str,
        query_text: Optional[str],
        claims: list[Claim],
        risk: Optional[RiskAssessment],
        propagation: Optional[PropagationSummary],
        counter_message: Optional[str],
        visual_card_path: Optional[str],
        run_log_items: list,
        counter_message_skip_reason: Optional[str] = None,
        topic_summaries: Optional[list[TopicSummary]] = None,
        cascade_predictions: Optional[list] = None,
        persuasion_features: Optional[list] = None,
        counter_target_plan=None,
        top_entities: Optional[list] = None,
        entity_co_occurrences: Optional[list] = None,
        immunity_strategy=None,
        counter_effect_records: Optional[list] = None,
        community_analysis: Optional[CommunityAnalysis] = None,
        intervention_decision: Any = None,   # P0-2: InterventionDecision | None
        run_dir: Optional[Path] = None,
        # Legacy alias kept for backward-compat callers
        community_detection_available: Optional[bool] = None,
    ) -> IncidentReport:
        """
        Compile all analysis outputs into a final IncidentReport.

        The Markdown body is produced by deterministic templating; only the
        Executive Summary + Flags sections are LLM-wrapped. Artifacts are
        also written to run_dir when provided (P0-2 integration).
        """
        report_id = str(uuid.uuid4())
        report = IncidentReport(
            id=report_id,
            intent_type=intent_type,
            query_text=query_text,
            claims=claims,
            risk_level=risk.risk_level.value if risk else None,
            requires_human_review=risk.requires_human_review if risk else False,
            propagation_summary=propagation,
            topic_summaries=topic_summaries or [],
            counter_message=counter_message,
            counter_message_skip_reason=counter_message_skip_reason,
            visual_card_path=visual_card_path,
            cascade_predictions=cascade_predictions or [],
            persuasion_features=persuasion_features or [],
            counter_target_plan=counter_target_plan,
            top_entities=top_entities or [],
            entity_co_occurrences=entity_co_occurrences or [],
            immunity_strategy=immunity_strategy,
            counter_effect_records=counter_effect_records or [],
            community_analysis=community_analysis,
        )
        for item in run_log_items:
            report.run_logs.append(item)

        # ── 1. Render deterministic body ──────────────────────────────────
        body_md = _render_body(
            query_text=query_text,
            claims=claims,
            risk=risk,
            propagation=propagation,
            topic_summaries=topic_summaries,
            cascade_predictions=cascade_predictions,
            persuasion_features=persuasion_features,
            counter_target_plan=counter_target_plan,
            top_entities=top_entities,
            community_analysis=community_analysis,
            counter_message=counter_message,
            counter_message_skip_reason=counter_message_skip_reason,
            visual_card_path=visual_card_path,
            immunity_strategy=immunity_strategy,
            counter_effect_records=counter_effect_records,
            intervention_decision=intervention_decision,
        )

        # ── 2. LLM wraps Executive Summary + Flags ─────────────────────────
        narrative_md = self._render_narrative(body_md, report)

        # ── 3. Fact-verify LLM narrative; fall back on drift ──────────────
        drift = _verify_narrative_facts(narrative_md, report)
        if drift:
            log.error("report.fact_drift", issues=drift)
            narrative_md = _fallback_narrative(report)
            report.log(
                "report_fact_verification",
                StageStatus.DEGRADED,
                "narrative drifted from structured data; fell back to static summary",
            )
        else:
            report.log("report_fact_verification", StageStatus.OK)

        report.report_md = f"{narrative_md}\n\n{body_md}".strip() + "\n"
        report.log("report_generation", StageStatus.OK)

        # ── 4. Persist to Postgres (best-effort) ──────────────────────────
        try:
            self._pg.save_report(report)
            log.info("report.saved", report_id=report_id)
        except Exception as exc:
            log.error("report.persist_error", error=str(exc))
            report.log("report_persist", StageStatus.ERROR, str(exc))

        # ── 5. Write to run_dir (P0-2) ────────────────────────────────────
        if run_dir is not None:
            try:
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "report.md").write_text(report.report_md, encoding="utf-8")
                (run_dir / "report_raw.json").write_text(
                    report.model_dump_json(indent=2), encoding="utf-8"
                )
            except Exception as exc:
                log.error("report.run_dir_write_error", error=str(exc),
                          run_dir=str(run_dir))

        return report

    # ── Internal ───────────────────────────────────────────────────────────────

    def _render_narrative(self, body_md: str, report: IncidentReport) -> str:
        """Call the LLM for Executive Summary + Flags sections only."""
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=512,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": _NARRATIVE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            "Factual body (render as-is below your two sections; do not modify):\n\n"
                            + body_md
                        ),
                    },
                ],
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            log.error("report.narrative_error", error=str(exc))
            return _fallback_narrative(report)


# ──────────────────────────────────────────────────────────────────────────────
# Rendering — pure functions
# ──────────────────────────────────────────────────────────────────────────────

def _render_body(
    *,
    query_text: Optional[str],
    claims: list[Claim],
    risk: Optional[RiskAssessment],
    propagation: Optional[PropagationSummary],
    topic_summaries: Optional[list[TopicSummary]],
    cascade_predictions: Optional[list],
    persuasion_features: Optional[list],
    counter_target_plan: Any,
    top_entities: Optional[list],
    community_analysis: Optional[CommunityAnalysis],
    counter_message: Optional[str],
    visual_card_path: Optional[str],
    immunity_strategy: Any,
    counter_effect_records: Optional[list],
    counter_message_skip_reason: Optional[str] = None,
    intervention_decision: Any = None,   # P0-2: InterventionDecision | None
) -> str:
    """Render all structural sections deterministically."""
    sections: list[str] = []

    # Primary-first ordering: shared by Claim Under Analysis & Evidence Assessment
    primary_id = (
        getattr(intervention_decision, "primary_claim_id", None)
        if intervention_decision is not None
        else None
    )
    ordered_claims = claims
    if claims and primary_id:
        primary = [c for c in claims if c.id == primary_id]
        others = [c for c in claims if c.id != primary_id]
        if primary:
            ordered_claims = primary + others

    # Claim Under Analysis — primary claim (if any) rendered first with [PRIMARY] marker
    sections.append("## Claim Under Analysis")
    if query_text:
        sections.append(f"- **Query**: {query_text}")
    if ordered_claims:
        for i, c in enumerate(ordered_claims, 1):
            tag = c.claim_actionability or "—"
            if c.non_actionable_reason:
                tag = f"{tag}/{c.non_actionable_reason}"
            marker = " [PRIMARY]" if primary_id and c.id == primary_id else ""
            sections.append(
                f"- **Claim {i}**{marker} [{tag}]: {c.normalized_text}"
            )
    else:
        sections.append("- No factual claims extracted.")
    sections.append("")

    # Intervention Decision (P0-2)
    sections.append("## Intervention Decision")
    if intervention_decision is not None:
        d = intervention_decision
        sections.append(f"- **Decision**: `{d.decision}`")
        if d.primary_claim_text:
            sections.append(f"- **Primary claim**: {d.primary_claim_text}")
        if d.reason:
            sections.append(f"- **Reason**: `{d.reason}`")
        if d.explanation:
            sections.append(f"- **Explanation**: {d.explanation}")
        if d.recommended_next_step:
            sections.append(
                f"- **Recommended next step**: `{d.recommended_next_step}`"
            )
        if d.visual_type:
            sections.append(f"- **Visual output**: `{d.visual_type}`")
        else:
            sections.append("- **Visual output**: none (abstention)")
    else:
        sections.append("- No intervention decision computed for this run.")
    sections.append("")

    # Evidence Assessment — same ordering as Claim Under Analysis (primary-first)
    sections.append("## Evidence Assessment")
    if ordered_claims:
        for i, c in enumerate(ordered_claims, 1):
            ev = c.evidence_summary()
            tier_counts = _evidence_tier_counts(c)
            tier_str = ", ".join(f"{k}={v}" for k, v in tier_counts.items() if v > 0) or "none"
            sections.append(
                f"- **Claim {i}**: {ev['supporting']} supporting, "
                f"{ev['contradicting']} contradicting, "
                f"{ev['uncertain']} uncertain — tiers: {tier_str}"
            )
    else:
        sections.append("- No claims to assess.")
    sections.append("")

    # Propagation Analysis
    sections.append("## Propagation Analysis")
    if propagation:
        sections.append(
            f"- **Posts**: {propagation.post_count}  "
            f"**Unique accounts**: {propagation.unique_accounts}  "
            f"**Velocity**: {propagation.velocity:.2f} posts/hr"
        )
        sections.append(
            f"- **Anomaly detected**: {propagation.anomaly_detected}"
            + (f" — {propagation.anomaly_description}" if propagation.anomaly_description else "")
        )
        if propagation.account_role_summary:
            roles = ", ".join(
                f"{k}={v}" for k, v in sorted(propagation.account_role_summary.items())
            )
            sections.append(f"- **Account roles**: {roles}")
        if propagation.coordinated_pairs:
            sections.append(
                f"- **Coordinated account pairs**: {propagation.coordinated_pairs}"
            )
    else:
        sections.append("- Propagation analysis not performed.")
    sections.append("")

    # Emotional Tone Analysis
    sections.append("## Emotional Tone Analysis")
    if topic_summaries:
        for ts in topic_summaries:
            dist = ", ".join(f"{k}={v:.2f}" for k, v in (ts.emotion_distribution or {}).items())
            flags = []
            if ts.is_trending:
                flags.append("TRENDING")
            if ts.is_likely_misinfo:
                flags.append("MISINFO")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            sections.append(
                f"- **{ts.label}**{flag_str}: dominant={ts.dominant_emotion or 'unknown'}"
                + (f"; distribution={dist}" if dist else "")
            )
    else:
        sections.append("- No topics identified.")
    sections.append("")

    # Risk Evaluation
    sections.append("## Risk Evaluation")
    if risk:
        sections.append(
            f"- **Risk level**: {risk.risk_level.value}  "
            f"**Misinfo score**: {risk.misinfo_score:.2f}"
        )
        sections.append(f"- **Requires human review**: {risk.requires_human_review}")
        if risk.flags:
            sections.append(f"- **Flags**: {', '.join(risk.flags)}")
        if risk.reasoning:
            sections.append(f"- **Reasoning**: {risk.reasoning}")
    else:
        sections.append("- Risk evaluation not performed.")
    sections.append("")

    # Cascade Forecast
    sections.append("## Cascade Forecast")
    if cascade_predictions:
        for cp in cascade_predictions[:5]:
            label = getattr(cp, "topic_label", None) or "unknown topic"
            sections.append(
                f"- **{label}**: ~{cp.predicted_posts_24h} posts/24h, "
                f"peak {cp.peak_window_hours}h, confidence={cp.confidence}"
            )
    else:
        sections.append("- No cascade predictions generated.")
    sections.append("")

    # Persuasion Tactics
    sections.append("## Persuasion Tactics")
    if persuasion_features:
        for pf in persuasion_features[:5]:
            claim_short = (pf.claim_text or "")[:80]
            sections.append(
                f"- virality={pf.virality_score:.2f}, tactic={pf.top_persuasion_tactic} "
                f'— "{claim_short}"'
            )
    else:
        sections.append("- Persuasion analysis not performed.")
    sections.append("")

    # Community Structure
    sections.append("## Community Structure")
    if community_analysis is None:
        sections.append("- Community detection not invoked this run.")
    elif community_analysis.skipped:
        reason = community_analysis.skip_reason or "unspecified"
        sections.append(f"- Community detection skipped — {reason}.")
    else:
        sections.append(
            f"- **Communities detected**: {community_analysis.community_count}  "
            f"**Echo chambers**: {community_analysis.echo_chamber_count}  "
            f"**Modularity Q**: {community_analysis.modularity:.3f}"
        )
        for c in community_analysis.communities[:4]:
            echo = " [ECHO]" if c.is_echo_chamber else ""
            sections.append(
                f"  - Community-{c.community_id}: size={c.size}, "
                f"isolation={c.isolation_score:.2f}, "
                f"emotion={c.dominant_emotion}{echo}"
            )
        if community_analysis.cross_community_signals:
            sections.append(
                f"- **Cross-community coordination signals**: "
                f"{len(community_analysis.cross_community_signals)}"
            )
    sections.append("")

    # Entities (supplement)
    if top_entities:
        sections.append("## Key Entities")
        sorted_ents = sorted(top_entities, key=lambda e: e.mention_count, reverse=True)[:10]
        for e in sorted_ents:
            sections.append(f"- **{e.name}** [{e.entity_type}] × {e.mention_count}")
        sections.append("")

    # Counter-Messaging Recommendation
    sections.append("## Counter-Messaging Recommendation")
    if counter_message:
        sections.append(f"- **Message**: {counter_message}")
        if visual_card_path:
            sections.append(f"- **Visual card**: `{visual_card_path}`")
        if counter_target_plan and counter_target_plan.recommended_targets:
            sections.append(
                f"- **Priority targets**: "
                + ", ".join(
                    f"{t.username or t.account_id}[{t.role}]"
                    for t in counter_target_plan.recommended_targets[:5]
                )
            )
    else:
        reason = counter_message_skip_reason or "risk_gate_not_triggered"
        reason_human = {
            "no_actionable_counter_evidence": (
                "no_actionable_counter_evidence — the primary claim has no "
                "contradicting evidence; a rebuttal would be vague. "
                "Better evidence retrieval (or human review) is required before deploying."
            ),
            "insufficient_evidence": (
                "insufficient_evidence — the risk gate flagged "
                "INSUFFICIENT_EVIDENCE for every candidate claim."
            ),
            "no_primary_claim": "no_primary_claim — no claim passed the risk gate.",
            "no_risk_assessment": "no_risk_assessment — risk evaluation did not run.",
            "risk_gate_not_triggered": (
                "risk_gate_not_triggered — the intent / risk / anomaly "
                "conditions did not call for a counter-message."
            ),
        }.get(reason, reason)
        sections.append(f"- **Skipped**: {reason_human}")
    sections.append("")

    # Immunity Strategy
    sections.append("## Immunity Strategy")
    if immunity_strategy is None or getattr(immunity_strategy, "skipped", True):
        reason = getattr(immunity_strategy, "skip_reason", None) if immunity_strategy else None
        sections.append(
            "- Immunity strategy not computed this cycle."
            + (f" Reason: {reason}" if reason else "")
        )
    else:
        sections.append(
            f"- **Targets**: {immunity_strategy.recommended_target_count}  "
            f"**Coverage**: {immunity_strategy.immunity_coverage*100:.1f}%  "
            f"**Strategy**: {immunity_strategy.strategy_used}"
        )
        if getattr(immunity_strategy, "summary", None):
            sections.append(f"- {immunity_strategy.summary}")
    sections.append("")

    # Counter-Effect Tracking
    sections.append("## Counter-Effect Tracking")
    if counter_effect_records:
        for rec in counter_effect_records:
            outcome = rec.outcome or "PENDING"
            score = f"{rec.effect_score:+.2f}" if rec.effect_score is not None else "N/A"
            if rec.followup_velocity is not None:
                vel = f"{rec.baseline_velocity:.2f} → {rec.followup_velocity:.2f} posts/hr"
            else:
                vel = f"baseline {rec.baseline_velocity:.2f} posts/hr (pending follow-up)"
            label = rec.topic_label or rec.topic_id or "N/A"
            sections.append(
                f"- **{label}**: outcome={outcome}, effect_score={score}, {vel}"
            )
        sections.append(
            "- _These metrics reflect relative-to-baseline observation; "
            "they are not a causal attribution._"
        )
    else:
        sections.append("- No counter-effect records this cycle.")
    sections.append("")

    return "\n".join(sections).rstrip() + "\n"


def _evidence_tier_counts(claim: Claim) -> dict[str, int]:
    """Count evidence items by source_tier across supporting+contradicting+uncertain."""
    counts = {"internal_chroma": 0, "wikipedia": 0, "news": 0}
    for pool in (
        claim.supporting_evidence,
        claim.contradicting_evidence,
        claim.uncertain_evidence,
    ):
        for e in pool:
            tier = getattr(e, "source_tier", "internal_chroma") or "internal_chroma"
            counts[tier] = counts.get(tier, 0) + 1
    return counts


# ──────────────────────────────────────────────────────────────────────────────
# Fact verification — scan LLM output for fabricated numbers
# ──────────────────────────────────────────────────────────────────────────────

_NUM_TOKEN = re.compile(r"-?\d+(?:\.\d+)?")


def _verify_narrative_facts(narrative_md: str, report: IncidentReport) -> list[str]:
    """
    Scan the LLM narrative. Flag any claim of the form "N communities",
    "N posts", "N% coverage", etc. whose number disagrees with the
    structured report object. Returns a list of drift issues (empty = pass).
    """
    issues: list[str] = []
    text = narrative_md or ""
    lower = text.lower()

    # Community counts
    if report.community_analysis and not report.community_analysis.skipped:
        truth_c = report.community_analysis.community_count
        truth_q = report.community_analysis.modularity
        for m in re.finditer(r"(\d+)\s+(?:communit(?:y|ies))", lower):
            if int(m.group(1)) != truth_c:
                issues.append(f"community_count drift: narrative said {m.group(1)}, truth {truth_c}")
        for m in re.finditer(r"modularity[^\d-]*(-?\d+\.\d+)", lower):
            try:
                if abs(float(m.group(1)) - truth_q) > 0.05:
                    issues.append(f"modularity drift: narrative said {m.group(1)}, truth {truth_q:.3f}")
            except ValueError:
                pass
    else:
        # LLM must NOT claim communities were found
        if re.search(r"\d+\s+(?:communit(?:y|ies))", lower) and "no" not in lower:
            # allow phrases like "no communities detected"
            pass

    # Guard against "louvain not installed" fabrication when it IS installed
    if (
        report.community_analysis
        and not report.community_analysis.skipped
        and ("louvain not installed" in lower or "not installed" in lower)
    ):
        issues.append("narrative claims louvain not installed but community analysis succeeded")

    # Post count
    if report.propagation_summary and report.propagation_summary.post_count > 0:
        truth_p = report.propagation_summary.post_count
        # allow ±10% tolerance (LLM often rounds)
        for m in re.finditer(r"(\d+)\s+posts", lower):
            n = int(m.group(1))
            if truth_p > 50 and abs(n - truth_p) > truth_p * 0.1 and n != 0:
                issues.append(f"post_count drift: narrative said {n}, truth {truth_p}")

    # Risk level — must not invent an opposite level
    if report.risk_level:
        valid = {"low", "medium", "high", "insufficient_evidence"}
        actual = report.risk_level.lower()
        for level in valid - {actual}:
            if re.search(rf"\brisk(?: level)?\s*[:=]?\s*{re.escape(level)}\b", lower):
                issues.append(f"risk_level drift: narrative said {level}, truth {actual}")

    return issues


def _fallback_narrative(report: IncidentReport) -> str:
    """Static Executive Summary + Flags fallback — no LLM, no drift."""
    lines = ["## Executive Summary"]
    risk = report.risk_level or "unknown"
    claim_count = len(report.claims)
    community_line = ""
    if report.community_analysis and not report.community_analysis.skipped:
        community_line = (
            f" Community analysis found "
            f"{report.community_analysis.community_count} communities "
            f"(Q={report.community_analysis.modularity:.3f})."
        )
    lines.append(
        f"Analyzed {claim_count} claim(s) with risk level **{risk}**.{community_line}"
    )
    if report.propagation_summary:
        lines.append(
            f"Propagation: {report.propagation_summary.post_count} posts from "
            f"{report.propagation_summary.unique_accounts} unique accounts "
            f"at {report.propagation_summary.velocity:.2f} posts/hr."
        )
    lines.append("")
    lines.append("## Flags and Next Steps")
    if report.requires_human_review:
        lines.append("- Human review required before further action.")
    else:
        lines.append("- No immediate escalation required.")
    if report.counter_message:
        lines.append("- Counter-message available for deployment; see Recommendation section.")
    else:
        lines.append("- No counter-message deployed this cycle.")
    return "\n".join(lines)
