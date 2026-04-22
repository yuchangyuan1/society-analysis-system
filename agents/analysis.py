"""
Analysis Workspace — propagation-analyze + cascade-predict skills.

Responsibilities:
  - Compute propagation trend metrics (velocity, stance distribution)
  - Detect repetition patterns and anomaly signals
  - Produce structured PropagationSummary
  - Phase 0: Account role classification, emotion aggregation
  - Phase 2: Cascade prediction (Task 1.10)
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.post import Post
from models.persuasion import CascadePrediction
from models.immunity import ImmunizationTarget, ImmunityStrategy
from models.report import CoordinationPair, PropagationSummary, TopicSummary
from services.kuzu_service import KuzuService
from services.postgres_service import PostgresService

log = structlog.get_logger(__name__)

_ANALYSIS_SYSTEM = """You are a social media propagation analyst working on a misinformation detection platform.
Your role is to analyse patterns in posts — not to evaluate the truth of any claim, but to identify
narrative structure, stance signals, and coordination patterns so that fact-checkers can act.

Given a set of posts, identify:
1. Main narrative themes (1-3 sentences describing what topics/claims appear)
2. Stance distribution (rough percentage breakdown: pro-claim / counter-claim / neutral)
3. Any suspicious patterns (e.g. coordinated posting, identical wording, unnatural velocity)

You MUST respond with a single valid JSON object and nothing else.
Required keys:
  "themes": string (1-3 sentences),
  "stance_distribution": object with string keys and integer values (percentages summing to ~100),
  "anomaly": boolean,
  "anomaly_description": string or null

Example output:
{"themes": "Posts discuss 5G-COVID conspiracy claims and rebuttals.", "stance_distribution": {"pro_claim": 50, "counter_claim": 40, "neutral": 10}, "anomaly": false, "anomaly_description": null}"""


class AnalysisAgent:
    def __init__(self, pg: PostgresService, kuzu: KuzuService) -> None:
        self._pg = pg
        self._kuzu = kuzu
        self._claude = openai.OpenAI(api_key=OPENAI_API_KEY)

    # ── Skill: propagation-analyze ────────────────────────────────────────────

    def analyze_propagation(
        self,
        posts: list[Post],
        topic: Optional[str] = None,
        window_hours: int = 24,
    ) -> PropagationSummary:
        """
        Compute propagation signals for a set of posts about a topic/claim.
        Returns a structured PropagationSummary.
        """
        if not posts:
            return PropagationSummary(topic=topic, post_count=0)

        unique_accounts = len({p.account_id for p in posts})
        velocity = self._compute_velocity(posts, window_hours)
        stance_dist = self._simple_stance_distribution(posts)

        # LLM-assisted anomaly detection
        anomaly, anomaly_desc, themes = self._llm_analysis(posts[:30], topic)

        # Graph-based coordination detection
        coord_rows = self._kuzu.get_coordinated_accounts(min_shared_claims=2)
        coord_pairs = [
            CoordinationPair(
                account1=r["account1"],
                account2=r["account2"],
                shared_claim_count=r["shared_claim_count"],
                sample_claims=r["sample_claims"],
            )
            for r in coord_rows[:10]  # cap for report size
        ]
        if coord_pairs and not anomaly:
            anomaly = True
            anomaly_desc = (
                f"Graph analysis detected {len(coord_pairs)} account pair(s) "
                f"independently spreading the same claims — possible coordinated amplification. "
                f"Top pair: {coord_pairs[0].account1} & {coord_pairs[0].account2} "
                f"share {coord_pairs[0].shared_claim_count} claim(s)."
            )
        elif coord_pairs and anomaly_desc:
            anomaly_desc = (
                anomaly_desc.rstrip(".") +
                f" Additionally, graph analysis found {len(coord_pairs)} "
                f"coordinated account pair(s) sharing identical claims."
            )

        summary = PropagationSummary(
            topic=topic or themes[:80] if themes else topic,
            post_count=len(posts),
            unique_accounts=unique_accounts,
            velocity=velocity,
            stance_distribution=stance_dist,
            anomaly_detected=anomaly,
            anomaly_description=anomaly_desc,
            coordinated_pairs=len(coord_pairs),
            coordination_details=coord_pairs,
        )
        log.info(
            "analysis.propagation_summary",
            post_count=len(posts),
            velocity=velocity,
            anomaly=anomaly,
            coordinated_pairs=len(coord_pairs),
        )
        return summary

    # ── Skill: account-role-classify (Phase 0, Task 2.2) ─────────────────────

    def classify_account_roles(
        self,
        posts: list[Post],
        claims: list,   # list[Claim] — avoid circular import
        roles_out: Optional[dict] = None,
    ) -> dict[str, int]:
        """
        Classify every account that appears in `posts` into one of four roles:
          ORIGINATOR — account whose post is the first_seen_post for any claim
          AMPLIFIER  — account posting across 3+ distinct topics
          BRIDGE     — account posting in topics with opposing stance distributions
          PASSIVE    — all others

        Writes roles to Kuzu Account nodes and returns a summary count dict.

        If ``roles_out`` is provided, the per-account role mapping is
        deposited into it (account_id → role string). This lets callers who
        need both the count summary and the raw map avoid a second DB
        roundtrip. P0-4 uses this to compute bridge_influence_ratio.
        """
        if not posts:
            return {}

        # Build lookup: account_id → set of post IDs
        account_posts: dict[str, list[Post]] = defaultdict(list)
        for p in posts:
            account_posts[p.account_id].append(p)

        # Collect originator accounts (first seen post owner)
        originator_ids: set[str] = set()
        for c in claims:
            if getattr(c, "first_seen_post", None):
                # Find which account owns that post
                for p in posts:
                    if p.id == c.first_seen_post:
                        originator_ids.add(p.account_id)
                        break

        # Collect the topics each account's posts belong to
        account_topics: dict[str, set[str]] = defaultdict(set)
        for p in posts:
            rows = self._kuzu.get_post_topics(p.id)
            for row in rows:
                account_topics[p.account_id].add(row.get("topic_id", ""))
        account_topics = {k: {t for t in v if t} for k, v in account_topics.items()}

        roles: dict[str, str] = {}
        role_counts: Counter = Counter()

        for account_id in account_posts:
            if account_id in originator_ids:
                role = "ORIGINATOR"
            elif len(account_topics.get(account_id, set())) >= 3:
                role = "AMPLIFIER"
            elif self._is_bridge_account(account_id, account_topics.get(account_id, set()), posts):
                role = "BRIDGE"
            else:
                role = "PASSIVE"

            roles[account_id] = role
            role_counts[role] += 1
            self._kuzu.upsert_account_role(account_id, role)

        log.info(
            "analysis.account_roles",
            originators=role_counts["ORIGINATOR"],
            amplifiers=role_counts["AMPLIFIER"],
            bridges=role_counts["BRIDGE"],
            passive=role_counts["PASSIVE"],
        )
        if roles_out is not None:
            roles_out.clear()
            roles_out.update(roles)
        return dict(role_counts)

    # ── Skill: cascade-predict (Phase 2, Task 1.10) ──────────────────────────

    def predict_cascade(
        self,
        topic_summary: TopicSummary,
        community_analysis=None,   # CommunityAnalysis | None
    ) -> CascadePrediction:
        """
        Heuristic 24-hour cascade prediction for a topic.

        Feature set:
          - current_velocity:       topic posts/hr
          - emotion_weight:         fear=1.0, anger=0.8, disgust=0.5, neutral=0.1, hope=0.2
          - top_influencer_score:   approximated via propagation_count of top claim
          - community_isolation:    avg isolation across echo-chamber communities
          - bridge_account_count:   bridges that may carry the topic across communities

        Forecast model (heuristic linear extrapolation):
          base_growth = velocity * 24
          emotion_multiplier = 1 + emotion_weight * 2        (fear doubles rate)
          isolation_damper   = 1 - community_isolation * 0.4 (isolation caps spread)
          bridge_boost       = 1 + bridge_account_count * 0.1

          predicted_posts = base_growth * emotion_multiplier
                            * isolation_damper * bridge_boost * influencer_boost
        """
        _EMOTION_WEIGHT = {
            "fear":    1.0,
            "anger":   0.8,
            "disgust": 0.5,
            "hope":    0.2,
            "neutral": 0.1,
            "":        0.1,
        }

        velocity = topic_summary.velocity or 0.0
        emotion = topic_summary.dominant_emotion or "neutral"
        ew = _EMOTION_WEIGHT.get(emotion, 0.1)

        # Community signals
        avg_isolation = 0.0
        bridge_count = 0
        if community_analysis and not getattr(community_analysis, "skipped", True):
            comms = getattr(community_analysis, "communities", [])
            if comms:
                avg_isolation = sum(c.isolation_score for c in comms) / len(comms)
                bridge_count = sum(len(c.bridge_accounts) for c in comms)

        # Influencer proxy: misinfo_risk correlates with high-reach claims
        influencer_boost = 1.0 + topic_summary.misinfo_risk * 0.5

        # Forecast
        base = velocity * 24
        multiplier = (
            (1 + ew * 2)
            * (1 - avg_isolation * 0.4)
            * (1 + bridge_count * 0.1)
            * influencer_boost
        )
        predicted_posts = max(int(base * multiplier), int(topic_summary.post_count))

        # Predicted new communities: more bridges + low isolation → spreads further
        pred_new_comms = max(0, int(bridge_count * (1 - avg_isolation) * 1.5))

        # Peak window estimation
        if ew >= 0.8:
            peak_window = "0-4h"    # fear/anger peaks quickly
        elif ew >= 0.5:
            peak_window = "4-12h"
        else:
            peak_window = "12-24h"

        # Confidence
        data_points = topic_summary.post_count
        if data_points >= 20:
            confidence = "HIGH"
        elif data_points >= 8:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        reasoning = (
            f"velocity={velocity:.1f} posts/hr, emotion={emotion} (weight={ew:.1f}), "
            f"avg_isolation={avg_isolation:.2f}, bridges={bridge_count}, "
            f"multiplier={multiplier:.2f}x"
        )

        pred = CascadePrediction(
            topic_id=topic_summary.topic_id,
            topic_label=topic_summary.label,
            current_velocity=velocity,
            emotion_weight=ew,
            community_isolation=avg_isolation,
            bridge_account_count=bridge_count,
            predicted_posts_24h=predicted_posts,
            predicted_new_communities=pred_new_comms,
            peak_window_hours=peak_window,
            confidence=confidence,
            reasoning=reasoning,
        )
        log.info(
            "analysis.cascade_prediction",
            topic=topic_summary.label[:50],
            predicted_posts=predicted_posts,
            confidence=confidence,
            peak=peak_window,
        )
        return pred

    # ── Skill: immunity-strategy (Phase 3, Task 1.8) ─────────────────────────

    def recommend_immunity_strategy(
        self,
        community_analysis=None,   # CommunityAnalysis | None
        topic_id: Optional[str] = None,
        topic_label: str = "",
        max_targets: int = 10,
    ) -> ImmunityStrategy:
        """
        Select a minimal set of accounts for targeted inoculation (pre-bunking)
        to maximally reduce the spread of misinformation across the network.

        Strategy (betweenness + pagerank):
          1. Pull all accounts with their role from Kuzu.
          2. Use community analysis for bridge account lists and isolation scores.
          3. Score each account by: 0.5*betweenness + 0.3*pagerank + echo_bonus.
          4. Prioritise BRIDGE > AMPLIFIER > echo-chamber periphery.
          5. Estimate network immunity coverage.
        """
        try:
            import networkx as nx  # type: ignore
        except ImportError:
            return ImmunityStrategy(
                topic_id=topic_id,
                topic_label=topic_label,
                skipped=True,
                skip_reason="networkx not installed",
            )

        if community_analysis is None or getattr(community_analysis, "skipped", True):
            return ImmunityStrategy(
                topic_id=topic_id,
                topic_label=topic_label,
                skipped=True,
                skip_reason="community analysis unavailable",
            )

        communities = getattr(community_analysis, "communities", [])
        if not communities:
            return ImmunityStrategy(
                topic_id=topic_id,
                topic_label=topic_label,
                skipped=True,
                skip_reason="no communities detected",
            )

        # Build Account-Account graph from community membership
        G = nx.Graph()
        account_community: dict[str, str] = {}
        account_community_label: dict[str, str] = {}
        bridge_set: set[str] = set()

        for comm in communities:
            cid = comm.community_id
            clabel = comm.label
            accounts = comm.account_ids or []
            for acc in accounts:
                G.add_node(acc)
                account_community[acc] = cid
                account_community_label[acc] = clabel
            for i, a1 in enumerate(accounts):
                for a2 in accounts[i + 1:]:
                    G.add_edge(a1, a2)
            for b in comm.bridge_accounts:
                bridge_set.add(b)

        all_accounts = list(G.nodes())
        total = len(all_accounts)
        if total == 0:
            return ImmunityStrategy(
                topic_id=topic_id,
                topic_label=topic_label,
                skipped=True,
                skip_reason="no accounts in communities",
            )

        # Compute betweenness (expensive for large graphs — cap at 500 nodes)
        if total > 500:
            betweenness = {n: 0.0 for n in all_accounts}
        else:
            betweenness = nx.betweenness_centrality(G, normalized=True)

        pagerank = nx.pagerank(G, alpha=0.85)

        # Pull account roles from Kuzu
        role_map: dict[str, str] = {}
        try:
            role_rows = self._kuzu.get_account_roles()
            for row in role_rows:
                role_map[row["account_id"]] = row["role"]
        except Exception:
            pass

        _ROLE_WEIGHT = {"ORIGINATOR": 0.0, "BRIDGE": 0.8, "AMPLIFIER": 0.5, "PASSIVE": 0.1}

        targets: list[ImmunizationTarget] = []
        for acc_id in all_accounts:
            role = role_map.get(acc_id, "PASSIVE")
            if role == "ORIGINATOR":
                continue  # skip originators (targeting them is counterproductive)

            bc = betweenness.get(acc_id, 0.0)
            pr = pagerank.get(acc_id, 0.0)
            is_bridge = acc_id in bridge_set

            # Isolation bonus: accounts in isolated communities are higher-value targets
            comm_id = account_community.get(acc_id, "")
            isolation = 0.0
            for comm in communities:
                if comm.community_id == comm_id:
                    isolation = comm.isolation_score
                    break
            echo_bonus = isolation * 0.3 if is_bridge else 0.0

            role_bonus = _ROLE_WEIGHT.get(role, 0.1)
            priority = 0.5 * bc + 0.3 * pr + echo_bonus + 0.2 * role_bonus

            rationale = (
                f"role={role}, betweenness={bc:.3f}, pagerank={pr:.4f}"
                + (f", echo_chamber_bridge (isolation={isolation:.2f})" if is_bridge else "")
            )

            targets.append(ImmunizationTarget(
                account_id=acc_id,
                role=role,
                community_id=comm_id,
                community_label=account_community_label.get(acc_id, ""),
                betweenness_centrality=round(bc, 4),
                pagerank_score=round(pr, 5),
                is_echo_chamber_entry=is_bridge,
                priority_score=round(priority, 4),
                rationale=rationale,
                inoculation_message=(
                    "You may encounter misleading content on this topic. "
                    "Here's what verified sources say: [fact-check link]"
                ),
            ))

        # Rank by priority
        targets.sort(key=lambda t: t.priority_score, reverse=True)
        top_targets = targets[:max_targets]

        # Estimate coverage: 1 − ∏(1 − pr_i)
        coverage = 1.0
        for t in top_targets:
            coverage *= (1.0 - t.pagerank_score)
        immunity_coverage = round(min(1.0, 1.0 - coverage), 3)

        summary = (
            f"Recommending {len(top_targets)} inoculation target(s) out of "
            f"{total} accounts analysed. "
            f"Estimated network immunity coverage: {immunity_coverage*100:.1f}%. "
            f"Top target: account '{top_targets[0].account_id}' "
            f"(role={top_targets[0].role}, priority={top_targets[0].priority_score:.3f})."
            if top_targets else
            "No suitable inoculation targets found."
        )

        log.info(
            "analysis.immunity_strategy",
            topic_id=topic_id,
            total_accounts=total,
            recommended=len(top_targets),
            coverage=immunity_coverage,
        )
        return ImmunityStrategy(
            topic_id=topic_id,
            topic_label=topic_label,
            targets=top_targets,
            total_accounts_analyzed=total,
            recommended_target_count=len(top_targets),
            immunity_coverage=immunity_coverage,
            summary=summary,
        )

    # ── Skill: topic-analysis ─────────────────────────────────────────────────

    def analyze_topics(
        self,
        topics: list[dict],
        all_posts: list[Post],
        risk_agent,          # RiskAgent — avoid circular import with TYPE_CHECKING
    ) -> list[TopicSummary]:
        """
        For each topic cluster, compute:
          - post_count / velocity from posts linked to its claims
          - aggregated misinfo_risk from per-claim risk scores
          - trending / misinfo flags

        Returns TopicSummary list sorted by (trending+misinfo, risk score).
        """
        post_by_id = {p.id: p for p in all_posts}
        summaries: list[TopicSummary] = []

        for topic in topics:
            cluster_claims = topic.get("claims", [])
            label = topic.get("label", "Unknown topic")
            topic_id = topic.get("topic_id", "")

            # Collect posts via direct Post→Topic edge (populated during clustering)
            # Fall back to Claim→Post chain if direct edges aren't written yet.
            topic_post_ids: set[str] = set()
            direct_rows = self._kuzu.get_topic_posts(topic_id)
            if direct_rows:
                topic_post_ids.update(r["post_id"] for r in direct_rows)
            else:
                for claim in cluster_claims:
                    rows = self._kuzu.get_claim_posts(claim.id)
                    topic_post_ids.update(r["post_id"] for r in rows)

            topic_posts = [post_by_id[pid] for pid in topic_post_ids
                           if pid in post_by_id]
            velocity = (self._compute_velocity(topic_posts, window_hours=24)
                        if topic_posts else 0.0)

            # Risk aggregation
            misinfo_scores: list[float] = []
            all_flags: list[str] = []
            for claim in cluster_claims:
                if not claim.has_sufficient_evidence(min_items=1):
                    continue
                try:
                    r = risk_agent.assess_risk(claim)
                    misinfo_scores.append(r.misinfo_score)
                    all_flags.extend(r.flags)
                except Exception as exc:
                    log.warning("analysis.topic_risk_error",
                                claim_id=claim.id, error=str(exc))

            avg_risk = (sum(misinfo_scores) / len(misinfo_scores)
                        if misinfo_scores else 0.0)
            is_trending = len(topic_post_ids) >= 3 or velocity > 2.0
            is_misinfo = avg_risk >= 0.65

            # Phase 0: Emotion aggregation across topic's posts
            dominant_emotion, emotion_dist = self._aggregate_emotions(topic_posts)

            summaries.append(TopicSummary(
                topic_id=topic_id,
                label=label,
                claim_count=len(cluster_claims),
                post_count=len(topic_post_ids),
                velocity=round(velocity, 2),
                is_trending=is_trending,
                misinfo_risk=round(avg_risk, 3),
                is_likely_misinfo=is_misinfo,
                representative_claims=[
                    c.normalized_text[:100] for c in cluster_claims[:3]
                ],
                risk_flags=list(dict.fromkeys(all_flags))[:6],
                dominant_emotion=dominant_emotion,
                emotion_distribution=emotion_dist,
            ))
            log.info(
                "analysis.topic_summary",
                label=label,
                posts=len(topic_post_ids),
                velocity=round(velocity, 2),
                risk=round(avg_risk, 3),
                trending=is_trending,
                misinfo=is_misinfo,
            )

        # Hottest misinformation topics first
        summaries.sort(
            key=lambda t: (t.is_trending and t.is_likely_misinfo,
                           t.misinfo_risk, t.post_count),
            reverse=True,
        )
        return summaries

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _aggregate_emotions(
        posts: list[Post],
    ) -> tuple[str, dict[str, float]]:
        """
        Aggregate per-post emotions into a topic-level distribution.
        Returns (dominant_emotion, {emotion: fraction}) for classified posts.
        Falls back to 'neutral' when no posts have emotion set.
        """
        _VALID = {"fear", "anger", "hope", "disgust", "neutral"}
        counts: Counter = Counter()
        for p in posts:
            if p.emotion and p.emotion in _VALID:
                counts[p.emotion] += 1

        if not counts:
            return "neutral", {}

        total = sum(counts.values())
        dist = {e: round(v / total, 3) for e, v in counts.items()}
        dominant = counts.most_common(1)[0][0]
        return dominant, dist

    def _is_bridge_account(
        self,
        account_id: str,
        topic_ids: set[str],
        all_posts: list[Post],
    ) -> bool:
        """
        Heuristic: account is a BRIDGE if it posted in at least 2 topics
        whose stance distributions lean in opposite directions.
        """
        if len(topic_ids) < 2:
            return False

        # For each topic, compute a rough pro/counter ratio
        topic_stances: dict[str, str] = {}
        for tid in list(topic_ids)[:4]:  # cap to avoid O(n²)
            topic_posts = [
                p for p in all_posts
                if p.account_id == account_id
            ]
            if not topic_posts:
                continue
            dist = self._simple_stance_distribution(topic_posts)
            pro = dist.get("supportive", 0)
            con = dist.get("against", 0)
            if pro > con:
                topic_stances[tid] = "pro"
            elif con > pro:
                topic_stances[tid] = "con"

        stances = set(topic_stances.values())
        return "pro" in stances and "con" in stances

    @staticmethod
    def _compute_velocity(posts: list[Post], window_hours: int) -> float:
        """Posts per hour within the window.

        If timestamps are available, compute actual rate over the observed span
        (capped at window_hours). With only one timestamped post, assume it
        represents one event spread across the full window.
        """
        timed = [p for p in posts if p.posted_at is not None]
        if not timed:
            # No timestamps: report raw count / window as a rough estimate
            return round(len(posts) / window_hours, 2)
        if len(timed) == 1:
            # Can't compute a rate from a single point; use 1/window
            return round(1.0 / window_hours, 2)
        timestamps = sorted(p.posted_at for p in timed)
        elapsed_hours = max(
            (timestamps[-1] - timestamps[0]).total_seconds() / 3600.0, 0.01
        )
        return round(len(timed) / min(elapsed_hours, window_hours), 2)

    @staticmethod
    def _simple_stance_distribution(posts: list[Post]) -> dict[str, int]:
        """
        Rudimentary keyword-based stance detector.
        A full implementation would use an NLI model.
        """
        pro_kws = {"true", "right", "correct", "confirmed", "agree"}
        con_kws = {"false", "wrong", "fake", "misleading", "debunked", "misinformation"}
        counts: Counter = Counter()
        for post in posts:
            words = set(post.text.lower().split())
            if words & con_kws:
                counts["against"] += 1
            elif words & pro_kws:
                counts["supportive"] += 1
            else:
                counts["neutral"] += 1
        return dict(counts)

    def _llm_analysis(
        self, posts: list[Post], topic: Optional[str]
    ) -> tuple[bool, Optional[str], str]:
        """Returns (anomaly_bool, anomaly_description, themes_text)."""
        post_snippets = "\n".join(
            f"- [{p.account_id}]: {p.text[:200]}" for p in posts[:20]
        )
        context = f"Topic: {topic or 'unknown'}\n\nPosts:\n{post_snippets}"
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": _ANALYSIS_SYSTEM},
                    {"role": "user", "content": context},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            # Strip markdown code fences if present
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            # Extract JSON object if preceded by explanatory text
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            data = json.loads(raw) if raw else {}
            return (
                bool(data.get("anomaly", False)),
                data.get("anomaly_description"),
                data.get("themes", ""),
            )
        except Exception as exc:
            log.error("analysis.llm_error", error=str(exc))
            return False, None, ""
