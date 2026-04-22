"""
Precompute Pipeline — offline batch orchestration.

This module is the *offline* analytical backbone: the 24-stage batch
pipeline (ingestion → claim extraction → topic clustering → community
detection → risk assessment → counter-message → report artifacts). It is
**not** the online query planner — see `agents/planner.py` for that.

Hard-orchestrated workflow:
  intent routing → skill sequence → risk gate → critic gate →
  report schema → visual generation trigger

Soft-agentic inside each step (LLM reasoning within bounded stages).
"""
from __future__ import annotations

import json
import shutil
from enum import Enum
from pathlib import Path
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.claim import Claim
from models.immunity import ImmunityStrategy
from models.persuasion import (
    CascadePrediction, PersuasionFeatures,
    CounterTargetPlan, CounterTargetRec,
    NamedEntity, EntityCoOccurrence,
)
from models.report import IncidentReport, RunLog, StageStatus, TopicSummary
from models.risk_assessment import RiskLevel
from services.counter_effect_service import CounterEffectService
from services.manifest_service import hash_posts_snapshot
from services.metrics_service import MetricsService

from .analysis import AnalysisAgent
from .community import CommunityAgent
from .counter_message import CounterMessageAgent
from .critic import CriticAgent
from .ingestion import IngestionAgent
from .knowledge import KnowledgeAgent
from .report import ReportAgent
from .risk import RiskAgent
from .visual import VisualAgent

log = structlog.get_logger(__name__)


class IntentType(str, Enum):
    CLAIM_ANALYSIS = "CLAIM_ANALYSIS"
    IMAGE_POST_ANALYSIS = "IMAGE_POST_ANALYSIS"
    PROPAGATION_REPORT = "PROPAGATION_REPORT"
    MISINFO_RISK_REVIEW = "MISINFO_RISK_REVIEW"
    COUNTER_MESSAGE = "COUNTER_MESSAGE"
    TREND_ANALYSIS = "TREND_ANALYSIS"


_INTENT_SYSTEM = """You are an intent classifier for a social media misinformation analysis system.
Given a user query, classify the primary intent.

Intent types:
  CLAIM_ANALYSIS       — user wants to verify or analyze a specific claim
  IMAGE_POST_ANALYSIS  — user wants to analyze an image post for misinformation
  PROPAGATION_REPORT   — user wants a propagation/spread analysis of a topic
  MISINFO_RISK_REVIEW  — user wants a risk assessment of a claim or topic
  COUNTER_MESSAGE      — user wants a counter-message / clarification card
  TREND_ANALYSIS       — user wants to discover trending topics AND identify which are
                         misinformation across many posts (hot topics + rumor detection)

You may return multiple intents as a JSON array if the request combines types.
Return ONLY a JSON array of intent strings, e.g.: ["TREND_ANALYSIS", "COUNTER_MESSAGE"]"""

_KEYWORD_SYSTEM = """You are a social media search expert.
Given a topic or query, generate 4-5 short X (Twitter) search keywords or phrases
that will find the most relevant recent posts about this topic including misinformation variants.

Rules:
- Each keyword must be ≤ 5 words
- Include both the "pro-misinfo" framing and fact-check/debunk variants
- Return ONLY a JSON array of strings, e.g.: ["5G COVID", "5G health risks", "5G towers dangerous"]"""


class PrecomputePipeline:
    """
    Top-level orchestrator. Clients call run() with a user query.
    The planner classifies intent, routes to the correct skill sequence,
    enforces the risk gate and critic gate, and returns an IncidentReport.
    """

    def __init__(
        self,
        ingestion: IngestionAgent,
        knowledge: KnowledgeAgent,
        analysis: AnalysisAgent,
        risk: RiskAgent,
        counter_msg: CounterMessageAgent,
        critic: CriticAgent,
        report_agent: ReportAgent,
        visual: VisualAgent,
        news_service=None,
        community: Optional[CommunityAgent] = None,
    ) -> None:
        self._ingestion = ingestion
        self._knowledge = knowledge
        self._analysis = analysis
        self._risk = risk
        self._counter_msg = counter_msg
        self._critic = critic
        self._report_agent = report_agent
        self._visual = visual
        self._news_service = news_service
        self._community = community
        self._claude = openai.OpenAI(api_key=OPENAI_API_KEY)
        # Phase 3: Counter-effect tracking service (lazy init)
        self._counter_effect_svc: Optional[CounterEffectService] = None

    # ── Public entry point ────────────────────────────────────────────────────

    def run(
        self,
        query: str,
        posts: Optional[list] = None,
        image_url: Optional[str] = None,
        image_path: Optional[str] = None,
        jsonl_path: Optional[str] = None,
        channel: Optional[str] = None,
        channel_date=None,
        channel_days_back: int = 7,
        # Reddit parameters
        subreddits: Optional[list[str]] = None,
        reddit_query: Optional[str] = None,
        reddit_sort: str = "hot",
        reddit_days_back: int = 7,
        # Run artifact directory — when provided, report/metrics/visuals are
        # also written under this path (P0-2). None keeps legacy behaviour.
        run_dir: Optional[Path] = None,
    ) -> IncidentReport:
        """
        Main workflow entry point.
        Returns a fully populated IncidentReport.
        All failures are explicitly logged; no silent failure.
        """
        log.info("planner.run", query=query[:120])
        run_logs: list[RunLog] = []

        # ── Step 1: Intent classification ──────────────────────────────────
        intents = self._classify_intent(query)
        log.info("planner.intents", intents=intents)
        run_logs.append(RunLog(
            stage="intent_classification",
            status=StageStatus.OK,
            detail=str(intents),
        ))

        # ── Step 2: Post ingestion ─────────────────────────────────────────
        ingested_posts = []
        if channel:
            try:
                ingested_posts = self._ingestion.ingest_channel_today(
                    channel, date=channel_date, days_back=channel_days_back
                )
                run_logs.append(RunLog(
                    stage="ingestion",
                    status=StageStatus.OK,
                    detail=(f"Channel @{channel}: "
                            f"{len(ingested_posts)} posts "
                            f"(last {channel_days_back} day(s))"),
                ))
            except Exception as exc:
                log.error("planner.channel_ingest_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="ingestion", status=StageStatus.ERROR, detail=str(exc)
                ))
        elif jsonl_path:
            try:
                ingested_posts = self._ingestion.ingest_posts_from_jsonl(jsonl_path)
                run_logs.append(RunLog(
                    stage="ingestion",
                    status=StageStatus.OK,
                    detail=f"Loaded {len(ingested_posts)} posts from {jsonl_path}",
                ))
            except Exception as exc:
                log.error("planner.ingestion_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="ingestion", status=StageStatus.ERROR, detail=str(exc)
                ))
        elif reddit_query or subreddits:
            # ── Reddit ingestion ──────────────────────────────────────────
            try:
                if reddit_query and subreddits:
                    # Search within specific subreddits
                    ingested_posts = self._ingestion.ingest_reddit_search(
                        reddit_query,
                        subreddits=subreddits,
                        days_back=reddit_days_back,
                    )
                    detail = (f"Reddit search '{reddit_query[:40]}' in "
                              f"r/{'+'.join(subreddits)}: "
                              f"{len(ingested_posts)} posts")
                elif reddit_query:
                    # Search all of Reddit
                    ingested_posts = self._ingestion.ingest_reddit_search(
                        reddit_query,
                        days_back=reddit_days_back,
                    )
                    detail = (f"Reddit search '{reddit_query[:40]}': "
                              f"{len(ingested_posts)} posts")
                elif subreddits and len(subreddits) == 1:
                    # Single subreddit browse
                    ingested_posts = self._ingestion.ingest_subreddit(
                        subreddits[0],
                        sort=reddit_sort,
                        days_back=reddit_days_back,
                    )
                    detail = (f"r/{subreddits[0]} ({reddit_sort}, "
                              f"last {reddit_days_back}d): "
                              f"{len(ingested_posts)} posts")
                else:
                    # Multiple subreddits — aggregate
                    ingested_posts = self._ingestion.ingest_multi_subreddit(
                        subreddits=subreddits,
                        sort=reddit_sort,
                        days_back=reddit_days_back,
                    )
                    detail = (f"Multi-subreddit ({reddit_sort}, "
                              f"last {reddit_days_back}d): "
                              f"{len(ingested_posts)} posts")
                run_logs.append(RunLog(
                    stage="ingestion", status=StageStatus.OK, detail=detail
                ))
            except Exception as exc:
                log.error("planner.reddit_ingest_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="ingestion", status=StageStatus.ERROR, detail=str(exc)
                ))
        elif posts:
            ingested_posts = posts
        elif query and IntentType.TREND_ANALYSIS in intents:
            # Multi-keyword search for maximum coverage
            try:
                keywords = self._generate_search_keywords(query)
                log.info("planner.trend_keywords", keywords=keywords)
                ingested_posts = self._ingestion.ingest_posts_from_multi_keywords(
                    keywords, max_per_query=20
                )
                run_logs.append(RunLog(
                    stage="ingestion",
                    status=StageStatus.OK,
                    detail=(f"Multi-keyword fetch: {len(ingested_posts)} posts "
                            f"via {len(keywords)} queries"),
                ))
            except Exception as exc:
                log.warning("planner.x_api_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="ingestion", status=StageStatus.DEGRADED, detail=str(exc)
                ))
        elif query and IntentType.PROPAGATION_REPORT in intents:
            try:
                ingested_posts = self._ingestion.ingest_posts_from_query(
                    query, max_results=50
                )
                run_logs.append(RunLog(
                    stage="ingestion",
                    status=StageStatus.OK,
                    detail=f"Fetched {len(ingested_posts)} posts from X API",
                ))
            except Exception as exc:
                log.warning("planner.x_api_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="ingestion", status=StageStatus.DEGRADED, detail=str(exc)
                ))

        # ── Step 3: Image post processing ─────────────────────────────────
        # Run for IMAGE_POST_ANALYSIS (explicit) OR when ingested posts have
        # images (e.g. TREND_ANALYSIS on Reddit image posts).
        # Cap at 10 image posts per run to limit Vision API cost.
        _MAX_IMAGE_POSTS = 10
        image_posts = (
            [p for p in ingested_posts if p.has_image][:_MAX_IMAGE_POSTS]
            if ingested_posts else []
        )
        # For explicit IMAGE_POST_ANALYSIS with a single image URL/path,
        # also process posts that don't yet have an ImageAsset.
        if IntentType.IMAGE_POST_ANALYSIS in intents and (image_url or image_path):
            for post in ingested_posts[:_MAX_IMAGE_POSTS]:
                if post not in image_posts:
                    image_posts.append(post)

        image_processed = 0
        for post in image_posts:
            try:
                self._ingestion.process_image_post(
                    post,
                    image_url=image_url if IntentType.IMAGE_POST_ANALYSIS in intents else None,
                    image_path=image_path if IntentType.IMAGE_POST_ANALYSIS in intents else None,
                )
                image_processed += 1
            except Exception as exc:
                log.error("planner.image_error", post_id=post.id, error=str(exc))

        if image_processed:
            run_logs.append(RunLog(
                stage="image_ingestion",
                status=StageStatus.OK,
                detail=f"Claude Vision processed {image_processed} image post(s)",
            ))

        # ── Step 3b: Emotion classification (Phase 0) ─────────────────────
        if ingested_posts:
            try:
                self._knowledge.classify_post_emotions(ingested_posts)
                run_logs.append(RunLog(
                    stage="emotion_classification",
                    status=StageStatus.OK,
                    detail=(
                        f"Classified emotions for "
                        f"{sum(1 for p in ingested_posts if p.emotion)} posts"
                    ),
                ))
            except Exception as exc:
                log.warning("planner.emotion_classify_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="emotion_classification",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 4: Claim extraction and deduplication ─────────────────────
        all_claims: list[Claim] = []
        if ingested_posts:
            for post in ingested_posts[:20]:  # cap for MVP
                try:
                    merged = post.merged_text()
                    claims = self._knowledge.extract_and_deduplicate_claims(
                        merged, post_id=post.id
                    )
                    all_claims.extend(claims)
                except Exception as exc:
                    log.error("planner.claim_extraction_error", error=str(exc))
        elif query:
            try:
                all_claims = self._knowledge.extract_and_deduplicate_claims(query)
            except Exception as exc:
                log.error("planner.claim_extraction_error", error=str(exc))

        # Deduplicate claim objects
        seen_ids = set()
        unique_claims: list[Claim] = []
        for c in all_claims:
            if c.id not in seen_ids:
                seen_ids.add(c.id)
                unique_claims.append(c)

        run_logs.append(RunLog(
            stage="claim_extraction",
            status=StageStatus.OK if unique_claims else StageStatus.DEGRADED,
            detail=f"{len(unique_claims)} unique claims extracted",
        ))

        # ── Step 5: Evidence retrieval ─────────────────────────────────────
        for i, claim in enumerate(unique_claims[:5]):
            try:
                unique_claims[i] = self._knowledge.build_evidence_pack(claim)
            except Exception as exc:
                log.error("planner.evidence_error", claim_id=claim.id, error=str(exc))

        # ── Step 5b: Actionability classification (P0-1, §10.3) ───────────
        # Rule-based. Runs after evidence retrieval so it can read
        # contradicting-evidence counts and tier distribution. Must run
        # before counter-messaging / intervention decisioning.
        from services.actionability_service import annotate_claims
        try:
            actionability_summary = annotate_claims(unique_claims)
            run_logs.append(RunLog(
                stage="claim_actionability",
                status=StageStatus.OK,
                detail=(
                    f"actionable={actionability_summary['actionable']}, "
                    f"non_actionable={actionability_summary['non_actionable']} "
                    f"(ctx_sparse={actionability_summary['context_sparse']}, "
                    f"insuf_ev={actionability_summary['insufficient_evidence']}, "
                    f"non_factual={actionability_summary['non_factual_expression']})"
                ),
            ))
        except Exception as exc:
            log.error("planner.actionability_error", error=str(exc))
            run_logs.append(RunLog(
                stage="claim_actionability",
                status=StageStatus.DEGRADED,
                detail=str(exc),
            ))

        # ── Step 6: Propagation analysis ──────────────────────────────────
        propagation = None
        if IntentType.PROPAGATION_REPORT in intents or ingested_posts:
            try:
                topic = unique_claims[0].normalized_text[:80] if unique_claims else query
                propagation = self._analysis.analyze_propagation(
                    ingested_posts, topic=topic
                )
                run_logs.append(RunLog(
                    stage="propagation_analysis",
                    status=StageStatus.OK,
                    detail=f"velocity={propagation.velocity:.1f}/hr, "
                           f"anomaly={propagation.anomaly_detected}",
                ))
            except Exception as exc:
                log.error("planner.propagation_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="propagation_analysis",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 6a: Account role classification (Phase 0) ───────────────
        account_roles_map: dict[str, str] = {}
        if propagation and ingested_posts and unique_claims:
            try:
                role_summary = self._analysis.classify_account_roles(
                    ingested_posts, unique_claims, roles_out=account_roles_map
                )
                if role_summary and propagation:
                    propagation.account_role_summary = role_summary
                # P0-4: bridge_influence_ratio — fraction of posts authored by
                # accounts classified as BRIDGE. Always write (0.0 when no
                # bridge accounts exist) so metrics.json has a definite value.
                bridge_ids = {
                    aid for aid, role in account_roles_map.items()
                    if role == "BRIDGE"
                }
                total_posts = len(ingested_posts)
                if total_posts > 0:
                    bridge_post_count = sum(
                        1 for p in ingested_posts if p.account_id in bridge_ids
                    )
                    propagation.bridge_influence_ratio = round(
                        bridge_post_count / total_posts, 4
                    )
                else:
                    propagation.bridge_influence_ratio = 0.0
                run_logs.append(RunLog(
                    stage="account_role_classification",
                    status=StageStatus.OK,
                    detail=(
                        f"{role_summary} | bridge_ratio="
                        f"{propagation.bridge_influence_ratio:.3f}"
                    ),
                ))
            except Exception as exc:
                log.warning("planner.role_classify_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="account_role_classification",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 6b: Topic clustering + per-topic analysis (TREND_ANALYSIS) ─
        topic_summaries: list[TopicSummary] = []
        topics: list[dict] = []
        if IntentType.TREND_ANALYSIS in intents and len(unique_claims) >= 2:
            try:
                topics = self._knowledge.cluster_claims_into_topics(unique_claims)
                if topics:
                    topic_summaries = self._analysis.analyze_topics(
                        topics, ingested_posts, self._risk
                    )
                    run_logs.append(RunLog(
                        stage="topic_analysis",
                        status=StageStatus.OK,
                        detail=(
                            f"{len(topic_summaries)} topics discovered; "
                            f"trending+misinfo: "
                            f"{sum(1 for t in topic_summaries if t.is_trending and t.is_likely_misinfo)}"
                        ),
                    ))
                    log.info(
                        "planner.topics_analyzed",
                        count=len(topic_summaries),
                        labels=[t.label for t in topic_summaries[:5]],
                    )
            except Exception as exc:
                log.error("planner.topic_analysis_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="topic_analysis",
                    status=StageStatus.ERROR,
                    detail=str(exc),
                ))

        # ── Step 6c: Fetch authoritative evidence for all topics ─────────
        # Fetch evidence for every discovered topic (not just trending/misinfo
        # ones) so that claims from any topic can pass the risk gate.
        if (
            self._news_service is not None
            and topics
            and IntentType.TREND_ANALYSIS in intents
        ):
            if topics:
                try:
                    chunks_stored = self._knowledge.fetch_evidence_for_topics(
                        topics, self._news_service
                    )
                    run_logs.append(RunLog(
                        stage="evidence_fetch",
                        status=StageStatus.OK,
                        detail=(
                            f"Fetched authoritative articles for "
                            f"{len(topics)} topic(s); "
                            f"{chunks_stored} chunks stored in RAG"
                        ),
                    ))
                except Exception as exc:
                    log.error("planner.evidence_fetch_error", error=str(exc))
                    run_logs.append(RunLog(
                        stage="evidence_fetch",
                        status=StageStatus.DEGRADED,
                        detail=str(exc),
                    ))

                # ── Step 6d: Per-topic vector search for evidence ─────────
                # For each topic, embed the topic label and do ONE Chroma
                # query; distribute hits to all claims in that topic.
                # This replaces the old per-claim build_evidence_pack loop.
                try:
                    refreshed = 0

                    # Build a quick id→index lookup for O(1) updates
                    claim_idx: dict[str, int] = {
                        c.id: i for i, c in enumerate(unique_claims)
                    }

                    for t in topics:
                        refreshed += self._knowledge.build_evidence_for_topic(
                            t, claim_idx, unique_claims
                        )

                    # Propagate updated claim objects back into topic dicts
                    # so _render_topic_card can find the evidence
                    updated_by_id: dict[str, Claim] = {
                        c.id: c for c in unique_claims
                    }
                    for t in topics:
                        t["claims"] = [
                            updated_by_id.get(c.id, c)
                            for c in t.get("claims", [])
                        ]

                    if refreshed:
                        run_logs.append(RunLog(
                            stage="evidence_refresh",
                            status=StageStatus.OK,
                            detail=f"{refreshed} claim(s) gained evidence after per-topic search",
                        ))
                except Exception as exc:
                    log.error("planner.evidence_refresh_error", error=str(exc))

        # ── Step 7: Risk gate ─────────────────────────────────────────────
        # Evaluate top-3 claims; select the highest-risk one with evidence.
        primary_claim = None
        risk = None
        top_claims = unique_claims[:3]
        risk_results = []
        for _c in top_claims:
            if _c.has_sufficient_evidence(min_items=1):
                try:
                    _r = self._risk.assess_risk(_c, propagation)
                    risk_results.append((_c, _r))
                except Exception as exc:
                    log.error("planner.risk_error", claim_id=_c.id, error=str(exc))

        if risk_results:
            risk_results.sort(key=lambda x: x[1].misinfo_score, reverse=True)
            primary_claim, risk = risk_results[0]
            run_logs.append(RunLog(
                stage="risk_assessment",
                status=StageStatus.OK,
                detail=(
                    f"{risk.risk_level.value} score={risk.misinfo_score:.2f} "
                    f"(evaluated {len(risk_results)} of {len(top_claims)} claims)"
                ),
            ))
        elif unique_claims:
            # No claim had sufficient evidence; assess first claim to trigger gate
            primary_claim = unique_claims[0]
            try:
                risk = self._risk.assess_risk(primary_claim, propagation)
                run_logs.append(RunLog(
                    stage="risk_assessment",
                    status=StageStatus.DEGRADED,
                    detail=f"{risk.risk_level.value} — no claims had sufficient evidence",
                ))
            except Exception as exc:
                log.error("planner.risk_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="risk_assessment",
                    status=StageStatus.ERROR,
                    detail=str(exc),
                ))

        # ── Step 7b: Intervention decision (P0-2, §10.4) ──────────────────
        # Translate primary_claim.claim_actionability + non_actionable_reason
        # into a structured decision: rebut | evidence_context | abstain.
        from services.intervention_decision_service import build_intervention_decision
        intervention_decision = build_intervention_decision(primary_claim)
        run_logs.append(RunLog(
            stage="intervention_decision",
            status=StageStatus.OK,
            detail=(
                f"{intervention_decision.decision}"
                + (f" ({intervention_decision.reason})"
                   if intervention_decision.reason else "")
            ),
        ))

        # Flag insufficient evidence — does NOT return early.
        # Counter-message (Step 8) will be skipped via should_generate_counter,
        # but Steps 9b–9h (topic cards, community, cascade, etc.) run normally.
        insufficient_evidence = bool(
            risk and risk.risk_level == RiskLevel.INSUFFICIENT_EVIDENCE
        )
        if insufficient_evidence:
            log.warning("planner.insufficient_evidence_flagged")
            run_logs.append(RunLog(
                stage="risk_gate",
                status=StageStatus.BLOCKED,
                detail="INSUFFICIENT_EVIDENCE — counter-message skipped, analysis continues",
            ))

        # ── Step 8: Counter-message (with critic gate) ─────────────────────
        counter_message: Optional[str] = None
        counter_message_skip_reason: Optional[str] = None
        visual_path: Optional[str] = None

        # Generate counter-message when:
        #   - explicitly requested (COUNTER_MESSAGE intent), OR
        #   - propagation anomaly detected on a PROPAGATION_REPORT query
        # Skipped when evidence is insufficient.
        counter_explicitly_requested = IntentType.COUNTER_MESSAGE in intents
        anomaly_trigger = (
            IntentType.PROPAGATION_REPORT in intents
            and propagation
            and propagation.anomaly_detected
            and not risk.requires_human_review  # type: ignore[union-attr]
        ) if risk else False
        # TREND_ANALYSIS: auto-generate counter-message only when the primary
        # claim has actionable counter-evidence (≥1 contradicting). Uncertain-
        # only evidence produces vague "verify before sharing" content that
        # wastes SD generation without substantive rebuttal.
        trend_high_risk_trigger = (
            IntentType.TREND_ANALYSIS in intents
            and risk is not None
            and primary_claim is not None
            and primary_claim.has_actionable_counter_evidence()
            and risk.misinfo_score >= 0.5
        )

        should_generate_counter = (
            (counter_explicitly_requested or anomaly_trigger or trend_high_risk_trigger)
            and primary_claim is not None
            and risk is not None
            and not insufficient_evidence
        )
        if not should_generate_counter:
            if primary_claim is None:
                counter_message_skip_reason = "no_primary_claim"
            elif risk is None:
                counter_message_skip_reason = "no_risk_assessment"
            elif insufficient_evidence:
                counter_message_skip_reason = "insufficient_evidence"
            elif not primary_claim.has_actionable_counter_evidence():
                counter_message_skip_reason = "no_actionable_counter_evidence"
            elif not (
                counter_explicitly_requested
                or anomaly_trigger
                or trend_high_risk_trigger
            ):
                counter_message_skip_reason = "risk_gate_not_triggered"
            else:
                counter_message_skip_reason = "unknown"
            run_logs.append(RunLog(
                stage="counter_message",
                status=StageStatus.BLOCKED,
                detail=f"skipped: {counter_message_skip_reason}",
            ))
            log.info(
                "planner.counter_message_skipped",
                reason=counter_message_skip_reason,
            )
        if should_generate_counter:
            # Soft-agentic: counter_message_fn(feedback) → revised draft.
            # NOTE: feedback is passed as revision_feedback; the claim object
            # is never mutated (avoids corrupting the original Claim data).
            def cm_fn(feedback: str) -> str:
                return self._counter_msg.build_counter_message(
                    primary_claim, risk, revision_feedback=feedback
                )

            try:
                approved_msg, critic_result = self._critic.review_with_retry(
                    cm_fn, primary_claim
                )
                if critic_result.verdict == "APPROVED" and approved_msg:
                    counter_message = approved_msg
                    run_logs.append(RunLog(
                        stage="critic_gate",
                        status=StageStatus.OK,
                        detail="Counter-message approved",
                    ))
                else:
                    run_logs.append(RunLog(
                        stage="critic_gate",
                        status=StageStatus.BLOCKED,
                        detail=critic_result.feedback,
                    ))
                    log.warning("planner.counter_message_blocked",
                                feedback=critic_result.feedback)
            except Exception as exc:
                log.error("planner.counter_message_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="critic_gate", status=StageStatus.ERROR, detail=str(exc)
                ))

        # ── Step 9: Visual generation (hard dependency: critic must pass) ──
        if counter_message and primary_claim:
            try:
                report_id_hint = query[:20].replace(" ", "_")
                visual_path = self._visual.generate_clarification_card(
                    counter_message, primary_claim, report_id=report_id_hint
                )
                if visual_path:
                    run_logs.append(RunLog(
                        stage="visual_generation",
                        status=StageStatus.OK,
                        detail=visual_path,
                    ))
                else:
                    run_logs.append(RunLog(
                        stage="visual_generation",
                        status=StageStatus.DEGRADED,
                        detail="visual_card_unavailable",
                    ))
            except Exception as exc:
                log.error("planner.visual_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="visual_generation",
                    status=StageStatus.ERROR,
                    detail=str(exc),
                ))
        elif (
            intervention_decision.decision == "evidence_context"
            and primary_claim is not None
        ):
            # P0-3 §10.4: Evidence/Context card for non_actionable claims
            # with >=2 supporting-evidence items. Abstain case produces no PNG.
            try:
                report_id_hint = query[:20].replace(" ", "_")
                visual_path = self._visual.generate_evidence_context_card(
                    primary_claim, report_id=report_id_hint
                )
                if visual_path:
                    run_logs.append(RunLog(
                        stage="visual_generation",
                        status=StageStatus.OK,
                        detail=f"evidence_context: {visual_path}",
                    ))
                else:
                    run_logs.append(RunLog(
                        stage="visual_generation",
                        status=StageStatus.DEGRADED,
                        detail="evidence_context card unavailable",
                    ))
            except Exception as exc:
                log.error("planner.evidence_context_visual_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="visual_generation",
                    status=StageStatus.ERROR,
                    detail=str(exc),
                ))

        # ── Step 9b: Per-topic infographic cards ──────────────────────────
        topic_card_paths: list[str] = []
        if topics and topic_summaries:
            try:
                report_slug = query[:15].replace(" ", "_")
                topic_card_paths = self._visual.generate_topic_cards(
                    topic_summaries=topic_summaries,
                    topics=topics,
                    all_posts=ingested_posts,
                    report_id=report_slug,
                )
                if topic_card_paths:
                    run_logs.append(RunLog(
                        stage="topic_cards",
                        status=StageStatus.OK,
                        detail=(
                            f"{len(topic_card_paths)} topic card(s) generated: "
                            + ", ".join(
                                p.split("\\")[-1].split("/")[-1]
                                for p in topic_card_paths[:3]
                            )
                        ),
                    ))
                else:
                    run_logs.append(RunLog(
                        stage="topic_cards",
                        status=StageStatus.DEGRADED,
                        detail="No trending topics with posts found",
                    ))
            except Exception as exc:
                log.error("planner.topic_cards_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="topic_cards",
                    status=StageStatus.ERROR,
                    detail=str(exc),
                ))

        # ── Step 9c: Community detection (Phase 1) ────────────────────────
        community_analysis = None
        if self._community and IntentType.TREND_ANALYSIS in intents and ingested_posts:
            try:
                community_analysis = self._community.detect_communities(ingested_posts)
                if community_analysis.skipped:
                    run_logs.append(RunLog(
                        stage="community_detection",
                        status=StageStatus.DEGRADED,
                        detail=community_analysis.skip_reason or "skipped",
                    ))
                else:
                    run_logs.append(RunLog(
                        stage="community_detection",
                        status=StageStatus.OK,
                        detail=(
                            f"{community_analysis.community_count} communities, "
                            f"{community_analysis.echo_chamber_count} echo chambers, "
                            f"modularity={community_analysis.modularity:.3f}"
                        ),
                    ))
                    log.info(
                        "planner.community_done",
                        communities=community_analysis.community_count,
                        echo_chambers=community_analysis.echo_chamber_count,
                    )
            except Exception as exc:
                log.error("planner.community_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="community_detection",
                    status=StageStatus.ERROR,
                    detail=str(exc),
                ))

        # ── Step 9d: Cascade prediction (Phase 2, Task 1.10) ─────────────
        cascade_predictions: list[CascadePrediction] = []
        if topic_summaries and IntentType.TREND_ANALYSIS in intents:
            try:
                for ts in topic_summaries:
                    if ts.is_trending:
                        cp = self._analysis.predict_cascade(ts, community_analysis)
                        cascade_predictions.append(cp)
                if cascade_predictions:
                    run_logs.append(RunLog(
                        stage="cascade_prediction",
                        status=StageStatus.OK,
                        detail=(
                            f"{len(cascade_predictions)} topic(s) forecast; "
                            f"top: {cascade_predictions[0].topic_label[:40]} → "
                            f"{cascade_predictions[0].predicted_posts_24h} posts/24h "
                            f"[{cascade_predictions[0].confidence}]"
                        ),
                    ))
            except Exception as exc:
                log.warning("planner.cascade_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="cascade_prediction",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 9e: Persuasion feature analysis (Phase 2, Task 1.4) ─────
        persuasion_features: list[PersuasionFeatures] = []
        if unique_claims and IntentType.TREND_ANALYSIS in intents:
            try:
                persuasion_features = self._knowledge.analyze_persuasion(unique_claims[:5])
                if persuasion_features:
                    top = persuasion_features[0]
                    run_logs.append(RunLog(
                        stage="persuasion_analysis",
                        status=StageStatus.OK,
                        detail=(
                            f"{len(persuasion_features)} claim(s) analyzed; "
                            f"top tactic: {top.top_persuasion_tactic} "
                            f"(virality={top.virality_score:.2f})"
                        ),
                    ))
            except Exception as exc:
                log.warning("planner.persuasion_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="persuasion_analysis",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 9f: Entity extraction (Phase 2, Task 1.3 supplement) ────
        top_entities: list[NamedEntity] = []
        entity_co_occurrences: list[EntityCoOccurrence] = []
        if unique_claims and IntentType.TREND_ANALYSIS in intents:
            try:
                top_entities, entity_co_occurrences = self._knowledge.extract_entities(
                    unique_claims[:20]
                )
                run_logs.append(RunLog(
                    stage="entity_extraction",
                    status=StageStatus.OK,
                    detail=(
                        f"{len(top_entities)} entities, "
                        f"{len(entity_co_occurrences)} co-occurrence pairs"
                    ),
                ))
            except Exception as exc:
                log.warning("planner.entity_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="entity_extraction",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 9g: Counter-targeting plan (Phase 2, Task 3.3) ──────────
        counter_target_plan: Optional[CounterTargetPlan] = None
        if (
            IntentType.TREND_ANALYSIS in intents
            and propagation
            and propagation.account_role_summary
        ):
            try:
                counter_target_plan = self._build_counter_target_plan(
                    propagation, community_analysis
                )
                if counter_target_plan and counter_target_plan.recommended_targets:
                    run_logs.append(RunLog(
                        stage="counter_targeting",
                        status=StageStatus.OK,
                        detail=(
                            f"{len(counter_target_plan.recommended_targets)} target(s) "
                            f"recommended; strategy: {counter_target_plan.strategy_summary[:60]}"
                        ),
                    ))
            except Exception as exc:
                log.warning("planner.targeting_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="counter_targeting",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 9h: Immunity strategy (Phase 3, Task 1.8) ───────────────
        immunity_strategy: Optional[ImmunityStrategy] = None
        if (
            IntentType.TREND_ANALYSIS in intents
            and community_analysis
            and not getattr(community_analysis, "skipped", True)
        ):
            try:
                topic_id_for_imm = topic_summaries[0].topic_id if topic_summaries else None
                topic_lbl_for_imm = topic_summaries[0].label if topic_summaries else query[:60]
                immunity_strategy = self._analysis.recommend_immunity_strategy(
                    community_analysis=community_analysis,
                    topic_id=topic_id_for_imm,
                    topic_label=topic_lbl_for_imm,
                )
                if immunity_strategy and not immunity_strategy.skipped:
                    run_logs.append(RunLog(
                        stage="immunity_strategy",
                        status=StageStatus.OK,
                        detail=(
                            f"{immunity_strategy.recommended_target_count} inoculation target(s); "
                            f"coverage={immunity_strategy.immunity_coverage*100:.1f}%"
                        ),
                    ))
                else:
                    run_logs.append(RunLog(
                        stage="immunity_strategy",
                        status=StageStatus.DEGRADED,
                        detail=getattr(immunity_strategy, "skip_reason", "skipped"),
                    ))
            except Exception as exc:
                log.warning("planner.immunity_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="immunity_strategy",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 9i: Counter-effect tracking (Phase 3, Task 3.2) ─────────
        counter_effect_records = []
        # P0-4: Always scan for pending followups whenever we have propagation
        # data for a topic/claim we've seen before. Record the new deployment
        # only if a counter_message was actually produced this run.
        if propagation and (topic_summaries or unique_claims):
            try:
                if self._counter_effect_svc is None:
                    self._counter_effect_svc = CounterEffectService()

                # Build lookup keys for follow-up matching
                topic_ids_now = [t.topic_id for t in (topic_summaries or []) if t.topic_id]
                topic_labels_now = [t.label for t in (topic_summaries or []) if t.label]
                claim_ids_now = [c.id for c in (unique_claims or []) if getattr(c, "id", None)]

                # (a) Close prior pending records (P0-4 — cross-run follow-up)
                pending = self._counter_effect_svc.get_pending_by_keys(
                    topic_ids=topic_ids_now,
                    topic_labels=topic_labels_now,
                    claim_ids=claim_ids_now,
                )
                for old_rec in pending:
                    try:
                        # Prefer matching the specific topic's current metrics
                        followup_vel = propagation.velocity
                        followup_cnt = propagation.post_count
                        if old_rec.topic_id:
                            for _ts in topic_summaries or []:
                                if _ts.topic_id == old_rec.topic_id:
                                    followup_vel = _ts.velocity
                                    followup_cnt = _ts.post_count
                                    break
                        updated = self._counter_effect_svc.record_followup(
                            old_rec.record_id,
                            followup_velocity=followup_vel,
                            followup_post_count=followup_cnt,
                        )
                        counter_effect_records.append(updated)
                    except Exception as sub_exc:
                        log.warning(
                            "planner.counter_effect_followup_error",
                            record_id=old_rec.record_id, error=str(sub_exc),
                        )

                # (b) New baseline deployment (only if we produced a message)
                if counter_message:
                    topic_id_ce = None
                    topic_lbl_ce = None
                    if topic_summaries and primary_claim:
                        pc_text = primary_claim.normalized_text
                        for _ts in topic_summaries:
                            reps = _ts.representative_claims or []
                            if pc_text in reps or any(pc_text[:60] in r for r in reps):
                                topic_id_ce = _ts.topic_id
                                topic_lbl_ce = _ts.label
                                break
                    if topic_id_ce is None and topic_summaries:
                        topic_id_ce = topic_summaries[0].topic_id
                        topic_lbl_ce = topic_summaries[0].label
                    ce_rec = self._counter_effect_svc.record_deployment(
                        report_id="pending",   # filled in after build_report
                        counter_message=counter_message,
                        baseline_velocity=propagation.velocity,
                        baseline_post_count=propagation.post_count,
                        claim_id=getattr(primary_claim, "id", None),
                        topic_id=topic_id_ce,
                        topic_label=topic_lbl_ce,
                    )
                    counter_effect_records.append(ce_rec)

                run_logs.append(RunLog(
                    stage="counter_effect_tracking",
                    status=StageStatus.OK,
                    detail=(
                        f"{len(pending)} prior record(s) followed up; "
                        f"{len(counter_effect_records)} total in report"
                    ),
                ))
            except Exception as exc:
                log.warning("planner.counter_effect_error", error=str(exc))
                run_logs.append(RunLog(
                    stage="counter_effect_tracking",
                    status=StageStatus.DEGRADED,
                    detail=str(exc),
                ))

        # ── Step 9j: Copy visual artifacts into run_dir (P0-2) ────────────
        if run_dir is not None:
            run_visuals_dir = run_dir / "counter_visuals"
            run_visuals_dir.mkdir(parents=True, exist_ok=True)
            if visual_path:
                visual_path = _copy_into_run_dir(visual_path, run_visuals_dir)
            topic_card_paths = [
                _copy_into_run_dir(p, run_visuals_dir) for p in topic_card_paths
            ]

        # ── Step 10: Build and return report ──────────────────────────────
        report = self._report_agent.build_report(
            intent_type="|".join(intents),
            query_text=query,
            claims=unique_claims,
            risk=risk,
            propagation=propagation,
            topic_summaries=topic_summaries,
            counter_message=counter_message,
            counter_message_skip_reason=counter_message_skip_reason,
            visual_card_path=visual_path,
            run_log_items=run_logs,
            cascade_predictions=cascade_predictions,
            persuasion_features=persuasion_features,
            counter_target_plan=counter_target_plan,
            top_entities=top_entities,
            entity_co_occurrences=entity_co_occurrences,
            immunity_strategy=immunity_strategy,
            counter_effect_records=counter_effect_records,
            community_analysis=community_analysis,
            intervention_decision=intervention_decision,
            run_dir=run_dir,
        )
        report.topic_card_paths = topic_card_paths
        if community_analysis and not community_analysis.skipped:
            report.community_analysis = community_analysis
        # P0-2: attach structured intervention decision
        report.intervention_decision = intervention_decision

        # ── Reproducibility metadata (for ManifestService) ────────────────
        try:
            report.posts_snapshot_sha256 = hash_posts_snapshot(ingested_posts)
            report.post_count = len(ingested_posts)
        except Exception as exc:
            log.warning("planner.snapshot_hash_error", error=str(exc))

        # ── P0-4: role_risk_correlation (§10.5) ───────────────────────────
        # Positive = ORIGINATOR accounts more concentrated on high-risk topics.
        # None when either bucket is empty.
        role_risk_correlation = _compute_role_risk_correlation(
            topic_summaries=topic_summaries,
            topics=topics,
            posts=ingested_posts,
            account_roles_map=account_roles_map,
        )

        # ── P0-5: Compute and persist metrics.json, append summary to MD ──
        if run_dir is not None:
            try:
                metrics_svc = MetricsService()
                metrics = metrics_svc.compute(
                    report=report,
                    claims=unique_claims,
                    counter_effect_service=self._counter_effect_svc,
                    run_id=run_dir.name,
                    role_risk_correlation=role_risk_correlation,
                )
                metrics_svc.write(run_dir, metrics)
                if report.report_md is not None:
                    report.report_md = (
                        report.report_md.rstrip()
                        + "\n\n"
                        + _render_social_snapshot(metrics)
                        + "\n\n"
                        + _render_run_metrics(metrics)
                        + "\n"
                    )
                    # Re-write updated report.md (same content as written by
                    # ReportAgent, but now with Run Metrics section appended).
                    try:
                        (run_dir / "report.md").write_text(
                            report.report_md, encoding="utf-8"
                        )
                        (run_dir / "report_raw.json").write_text(
                            report.model_dump_json(indent=2), encoding="utf-8"
                        )
                    except Exception as exc:
                        log.warning(
                            "planner.metrics_rewrite_error", error=str(exc)
                        )
            except Exception as exc:
                log.error("planner.metrics_error", error=str(exc))

        # Back-fill report_id in the counter-effect record
        if counter_effect_records and self._counter_effect_svc:
            try:
                first_rec = counter_effect_records[0]
                if first_rec.outcome == "PENDING":
                    import sqlite3
                    with self._counter_effect_svc._connect() as con:
                        con.execute(
                            "UPDATE counter_effect_records SET report_id=? WHERE record_id=?",
                            (report.id, first_rec.record_id),
                        )
                        con.commit()
            except Exception:
                pass

        return report

    # ── Internal ───────────────────────────────────────────────────────────────

    def _build_counter_target_plan(
        self,
        propagation,
        community_analysis=None,
    ) -> CounterTargetPlan:
        """
        Phase 2, Task 3.3 — Build an optimal counter-messaging target plan.

        Priority ranking (from ROADMAP.md):
          1. BRIDGE accounts (cross-community reach, highest immunity efficiency)
          2. High-trust accounts already exposed to misinformation
          3. Peripheral accounts in echo chambers (susceptible to outside info)
        Exclude: ORIGINATOR accounts (known spreaders, low persuasion chance).
        """
        role_summary = propagation.account_role_summary or {}
        originators = role_summary.get("ORIGINATOR", 0)
        bridges = role_summary.get("BRIDGE", 0)
        amplifiers = role_summary.get("AMPLIFIER", 0)
        passive = role_summary.get("PASSIVE", 0)

        # Fetch account roles from Kuzu for detailed list
        all_accounts = []
        try:
            rows = self._analysis._kuzu.get_account_roles()
            all_accounts = rows or []
        except Exception:
            pass

        excluded: list[str] = []
        targets: list[CounterTargetRec] = []
        rank = 1

        # Priority 1: BRIDGE accounts
        for acc in all_accounts:
            if acc.get("role") == "ORIGINATOR":
                excluded.append(acc.get("account_id", ""))
                continue
            if acc.get("role") == "BRIDGE":
                targets.append(CounterTargetRec(
                    account_id=acc.get("account_id", ""),
                    username=acc.get("username", ""),
                    role="BRIDGE",
                    priority_rank=rank,
                    rationale="Bridge account connects multiple communities — highest reach efficiency",
                ))
                rank += 1

        # Priority 2: AMPLIFIER accounts (high reach, exposed to misinformation)
        for acc in all_accounts:
            if acc.get("role") == "AMPLIFIER":
                targets.append(CounterTargetRec(
                    account_id=acc.get("account_id", ""),
                    username=acc.get("username", ""),
                    role="AMPLIFIER",
                    priority_rank=rank,
                    rationale="Amplifier account has broad topic reach — counter-message multiplier",
                ))
                rank += 1

        # Priority 3: PASSIVE accounts in echo chambers (peripheral, open to correction)
        if community_analysis and not getattr(community_analysis, "skipped", True):
            for comm in getattr(community_analysis, "communities", []):
                if comm.is_echo_chamber:
                    for bridge_id in comm.bridge_accounts[:3]:
                        already = {t.account_id for t in targets}
                        if bridge_id not in already and bridge_id not in excluded:
                            targets.append(CounterTargetRec(
                                account_id=bridge_id,
                                role="BRIDGE",
                                community_id=comm.community_id,
                                priority_rank=rank,
                                rationale=(
                                    f"Bridge into echo chamber '{comm.label}' "
                                    f"(isolation={comm.isolation_score:.2f})"
                                ),
                            ))
                            rank += 1

        # Strategy narrative
        if bridges > 0:
            strategy = (
                f"Prioritise {bridges} bridge account(s) for maximum cross-community reach. "
                f"Exclude {originators} originator(s)."
            )
        elif amplifiers > 0:
            strategy = (
                f"No bridge accounts detected. Target {amplifiers} amplifier(s) "
                f"with high topic coverage."
            )
        else:
            strategy = (
                f"Limited high-priority targets. Broad outreach to {passive} passive accounts recommended."
            )

        return CounterTargetPlan(
            recommended_targets=targets[:10],
            excluded_accounts=excluded[:20],
            strategy_summary=strategy,
        )

    def _classify_intent(self, query: str) -> list[str]:
        """Classify user intent; returns list of IntentType strings."""
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=64,
                messages=[
                    {"role": "system", "content": _INTENT_SYSTEM},
                    {"role": "user", "content": query},
                ],
            )
            raw = response.choices[0].message.content or "[]"
            parsed: list[str] = json.loads(raw)
            valid = [i for i in parsed if i in IntentType.__members__]
            if not valid:
                valid = [IntentType.CLAIM_ANALYSIS.value]
            return valid
        except Exception as exc:
            log.error("planner.intent_error", error=str(exc))
            return [IntentType.CLAIM_ANALYSIS.value]

    def _generate_search_keywords(self, query: str) -> list[str]:
        """Use LLM to expand a topic query into multiple X API search keywords."""
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=128,
                messages=[
                    {"role": "system", "content": _KEYWORD_SYSTEM},
                    {"role": "user", "content": query},
                ],
            )
            raw = (response.choices[0].message.content or "[]").strip()
            start, end = raw.find("["), raw.rfind("]") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            keywords: list[str] = json.loads(raw) if raw else []
            # Sanitize each keyword through the ingestion helper
            from .ingestion import IngestionAgent
            sanitized = [
                IngestionAgent._sanitize_search_query(kw)
                for kw in keywords if isinstance(kw, str)
            ]
            return sanitized[:5] or [query[:40]]
        except Exception as exc:
            log.error("planner.keyword_gen_error", error=str(exc))
            return [query[:40]]


def _compute_role_risk_correlation(
    *,
    topic_summaries: list,
    topics: list,
    posts: list,
    account_roles_map: dict,
) -> Optional[float]:
    """
    P0-4 §10.5: difference of ORIGINATOR-share between high-risk topics
    (misinfo_risk >= 0.6) and low-risk topics (misinfo_risk < 0.3).

    Returns a value in [-1.0, 1.0], or None if either bucket has no posts.
    """
    if not topic_summaries or not topics or not posts or not account_roles_map:
        return None

    topic_by_id = {t.get("topic_id"): t for t in topics}
    post_by_id = {p.id: p for p in posts}

    def posts_in_topic(topic_id: str) -> list:
        topic = topic_by_id.get(topic_id)
        if not topic:
            return []
        post_ids: set[str] = set()
        for c in topic.get("claims", []):
            fsp = getattr(c, "first_seen_post", None)
            if fsp and fsp in post_by_id:
                post_ids.add(fsp)
        return [post_by_id[pid] for pid in post_ids]

    def originator_share(ts_list) -> Optional[float]:
        collected: set[str] = set()
        for ts in ts_list:
            for p in posts_in_topic(ts.topic_id):
                collected.add(p.id)
        if not collected:
            return None
        orig = sum(
            1 for pid in collected
            if account_roles_map.get(post_by_id[pid].account_id) == "ORIGINATOR"
        )
        return orig / len(collected)

    high = [ts for ts in topic_summaries if ts.misinfo_risk >= 0.6]
    low = [ts for ts in topic_summaries if ts.misinfo_risk < 0.3]
    high_share = originator_share(high)
    low_share = originator_share(low)
    if high_share is None or low_share is None:
        return None
    return round(high_share - low_share, 4)


def _render_run_metrics(metrics: dict) -> str:
    """Append-only Markdown section summarising the metrics.json contents."""
    lines = ["## Run Metrics"]
    ev = metrics.get("evidence_coverage")
    ev_pct = f"{ev*100:.1f}%" if isinstance(ev, (int, float)) else "N/A"
    lines.append(
        f"- **Evidence coverage**: {ev_pct} "
        f"({metrics.get('evidence_with_any', 0)}/{metrics.get('evidence_total_claims', 0)} claims)"
    )
    tiers = metrics.get("evidence_tier_distribution") or {}
    if any(tiers.values()):
        tier_str = ", ".join(f"{k}={v}" for k, v in tiers.items() if v > 0)
        lines.append(f"- **Evidence tiers**: {tier_str}")
    q = metrics.get("community_modularity_q")
    if isinstance(q, (int, float)):
        lines.append(f"- **Community modularity Q**: {q:.3f}")
    roles = metrics.get("account_role_counts") or {}
    if roles:
        lines.append(
            "- **Account roles**: "
            + ", ".join(f"{k}={v}" for k, v in sorted(roles.items()))
        )
    clr = metrics.get("counter_effect_closed_loop_rate")
    if isinstance(clr, (int, float)):
        lines.append(
            f"- **Counter-effect closed-loop rate**: {clr*100:.1f}%"
        )
    # P0-1/P0-4 additions
    ad = metrics.get("actionability_distribution") or {}
    if ad:
        lines.append(
            "- **Actionability**: "
            f"actionable={ad.get('actionable', 0)}, "
            f"non_actionable={ad.get('non_actionable', 0)} "
            f"(ctx_sparse={ad.get('context_sparse', 0)}, "
            f"insuf_ev={ad.get('insufficient_evidence', 0)}, "
            f"non_factual={ad.get('non_factual_expression', 0)})"
        )
    lines.append(f"- _Computed at {metrics.get('computed_at', '')}_")
    return "\n".join(lines)


_BRIDGE_EXPLAIN = (
    "Share of posts authored by BRIDGE-role accounts. "
    "High values suggest cross-community amplification rather than organic in-group conversation."
)
_ROLE_RISK_EXPLAIN = (
    "Difference in ORIGINATOR-share between high-risk and low-risk topics. "
    "Positive values indicate originators concentrate on the risky end of the topic spectrum; "
    "null means one risk bucket had no topics."
)


def _render_social_snapshot(metrics: dict) -> str:
    """Dedicated '## Social Analysis Snapshot' section (C2).

    Kept separate from Run Metrics so the two graph-derived social signals are
    read as a small deliverable rather than buried among evidence / actionability
    lines. Fixture runs will surface 0.0 / null here — that is a correct-but-flat
    result, documented in demo_script.md.
    """
    lines = ["## Social Analysis Snapshot"]
    br = metrics.get("bridge_influence_ratio")
    if isinstance(br, (int, float)):
        lines.append(f"- **Bridge influence ratio**: {br:.3f}")
    else:
        lines.append("- **Bridge influence ratio**: N/A")
    lines.append(f"  _{_BRIDGE_EXPLAIN}_")
    rrc = metrics.get("role_risk_correlation")
    if rrc is None:
        lines.append("- **Role-risk correlation**: N/A (one risk bucket empty)")
    else:
        lines.append(f"- **Role-risk correlation**: {rrc:+.3f}")
    lines.append(f"  _{_ROLE_RISK_EXPLAIN}_")
    return "\n".join(lines)


def _copy_into_run_dir(src_path: str, dst_dir: Path) -> str:
    """Copy a visual artifact into run_dir/counter_visuals/; return new path string."""
    try:
        src = Path(src_path)
        if not src.exists():
            return src_path
        dst = dst_dir / src.name
        if src.resolve() == dst.resolve():
            return str(dst)
        shutil.copy2(src, dst)
        return str(dst)
    except Exception as exc:
        log.warning("planner.visual_copy_error", src=src_path, error=str(exc))
        return src_path
