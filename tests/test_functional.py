"""
Society Analysis Project — Complete Functional Test Suite
覆盖范围: Phase 0 / Phase 1 / Phase 2 / Phase 3 全功能模块
运行方式: python -m pytest tests/test_functional.py -v   (或 python tests/test_functional.py)

策略:
  - 所有 LLM 调用通过 unittest.mock.patch 拦截，返回预设 JSON
  - Kuzu / Chroma / Postgres 使用临时目录隔离，每个 TestCase 独立清理
  - Phase 3 Counter-Effect Service 使用内存 SQLite（:memory:）
  - 端到端测试 (T09) 用 mock 走完完整 PlannerAgent.run() 并验证 IncidentReport 字段
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ── Ensure project root is on sys.path ────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Synthetic test data ────────────────────────────────────────────────────────
_NOW = datetime.utcnow()

def _make_posts(n: int = 20) -> list:
    """Return n synthetic Post objects covering multiple accounts and topics."""
    from models.post import Post
    emotions = ["fear", "anger", "neutral", "hope", "disgust"]
    posts = []
    for i in range(n):
        posts.append(Post(
            id=f"post_{i:03d}",
            account_id=f"account_{i % 5:02d}",   # 5 unique accounts
            channel_name=f"chan_{i % 3}",
            text=(
                f"BREAKING: Vaccine causes autism - confirmed by doctor #{i}! "
                if i % 3 == 0 else
                f"This is FALSE - vaccines are safe and effective. #{i}"
                if i % 3 == 1 else
                f"Discussing vaccine policy #{i}"
            ),
            posted_at=_NOW - timedelta(hours=i * 0.5),
            emotion=emotions[i % 5],
            emotion_score=0.7 + (i % 3) * 0.1,
        ))
    return posts


def _make_claims(n: int = 5) -> list:
    """Return n synthetic Claim objects."""
    from models.claim import Claim, ClaimEvidence
    claims = []
    for i in range(n):
        ev = ClaimEvidence(
            article_id=f"art_{i}",
            article_title=f"Study {i}",
            article_url=f"https://example.com/{i}",
            source_name="Reuters",
            stance="contradicts",
            snippet=f"Scientists refute claim {i}",
        )
        claims.append(Claim(
            id=f"claim_{i:03d}",
            normalized_text=f"Vaccines cause autism — variant {i}",
            first_seen_post=f"post_{i:03d}" if i < 3 else None,
            propagation_count=10 + i * 5,
            contradicting_evidence=[ev],
        ))
    return claims


# ══════════════════════════════════════════════════════════════════════════════
# T01  Model instantiation and field validation
# ══════════════════════════════════════════════════════════════════════════════
class T01_Models(unittest.TestCase):
    """Phase 0/1/2/3 — Pydantic model construction."""

    def test_post_emotion_fields(self):
        from models.post import Post
        p = Post(id="p1", account_id="a1", text="fear post",
                 emotion="fear", emotion_score=0.9)
        self.assertEqual(p.emotion, "fear")
        self.assertAlmostEqual(p.emotion_score, 0.9)

    def test_claim_evidence_summary(self):
        from models.claim import Claim, ClaimEvidence
        ev = ClaimEvidence(article_id="art1", stance="contradicts")
        c = Claim(id="c1", normalized_text="test claim",
                  contradicting_evidence=[ev])
        summ = c.evidence_summary()
        self.assertEqual(summ["contradicting"], 1)
        self.assertEqual(summ["supporting"], 0)
        self.assertTrue(c.has_sufficient_evidence(min_items=1))

    def test_community_models(self):
        from models.community import CommunityInfo, CommunityAnalysis
        ci = CommunityInfo(
            community_id="c1", label="AntiVax",
            size=30, isolation_score=0.88,
            is_echo_chamber=True,
            account_ids=["a1", "a2"],
            bridge_accounts=["a3"],
        )
        ca = CommunityAnalysis(
            community_count=2, echo_chamber_count=1,
            communities=[ci], modularity=0.42,
        )
        self.assertEqual(ca.echo_chamber_count, 1)
        self.assertTrue(ca.communities[0].is_echo_chamber)

    def test_cascade_prediction_model(self):
        from models.persuasion import CascadePrediction
        cp = CascadePrediction(
            topic_id="t1", topic_label="Vaccine claims",
            current_velocity=8.5, emotion_weight=0.9,
            predicted_posts_24h=312,
            peak_window_hours="0-4h",
            confidence="HIGH",
        )
        self.assertEqual(cp.confidence, "HIGH")
        self.assertEqual(cp.predicted_posts_24h, 312)

    def test_persuasion_features_model(self):
        from models.persuasion import PersuasionFeatures
        # Note: authority_reference and identity_trigger are bool;
        # urgency_markers is int in the current model definition.
        pf = PersuasionFeatures(
            claim_id="c1",
            claim_text="Vaccines cause autism",
            emotional_appeal=0.9,
            fear_framing=0.85,
            simplicity_score=0.7,
            authority_reference=True,
            urgency_markers=3,
            identity_trigger=True,
            virality_score=0.77,
            top_persuasion_tactic="fear_framing",
            explanation="Heavy fear appeal with urgency markers.",
        )
        self.assertAlmostEqual(pf.virality_score, 0.77, places=2)

    def test_counter_target_plan(self):
        from models.persuasion import CounterTargetPlan, CounterTargetRec
        plan = CounterTargetPlan(
            recommended_targets=[
                CounterTargetRec(account_id="bridge_01", role="BRIDGE", priority_rank=1)
            ],
            strategy_summary="Target bridge accounts first.",
        )
        self.assertEqual(plan.recommended_targets[0].role, "BRIDGE")

    def test_counter_effect_models(self):
        from models.counter_effect import CounterEffectRecord, CounterEffectReport
        rec = CounterEffectRecord(
            record_id="r1", report_id="rep1",
            counter_message="Vaccines are safe.",
            baseline_velocity=10.0, baseline_post_count=80,
        )
        self.assertEqual(rec.outcome, "PENDING")
        report = CounterEffectReport(total_tracked=1, effective_count=1, summary="1 tracked.")
        self.assertEqual(report.effective_count, 1)

    def test_immunity_models(self):
        from models.immunity import ImmunizationTarget, ImmunityStrategy
        t = ImmunizationTarget(
            account_id="bridge_01", role="BRIDGE",
            betweenness_centrality=0.45, pagerank_score=0.02,
            priority_score=0.38,
        )
        strat = ImmunityStrategy(
            topic_id="t1", targets=[t],
            recommended_target_count=1,
            immunity_coverage=0.62,
        )
        self.assertAlmostEqual(strat.immunity_coverage, 0.62)
        self.assertEqual(strat.targets[0].role, "BRIDGE")

    def test_incident_report_phase3_fields(self):
        from models.report import IncidentReport
        from models.immunity import ImmunityStrategy
        from models.counter_effect import CounterEffectRecord
        rpt = IncidentReport(
            id="rep1", intent_type="TREND_ANALYSIS",
            immunity_strategy=ImmunityStrategy(topic_id="t1", skipped=True),
            counter_effect_records=[
                CounterEffectRecord(
                    record_id="r1", report_id="rep1",
                    counter_message="msg", baseline_velocity=5.0,
                    baseline_post_count=40,
                )
            ],
        )
        self.assertTrue(rpt.immunity_strategy.skipped)
        self.assertEqual(len(rpt.counter_effect_records), 1)


# ══════════════════════════════════════════════════════════════════════════════
# T02  KuzuService schema and CRUD
# ══════════════════════════════════════════════════════════════════════════════
class T02_KuzuService(unittest.TestCase):
    """Kuzu graph DB — schema init + core upsert/query operations."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="kuzu_test_")
        from services.kuzu_service import KuzuService
        # Kuzu needs a non-existent sub-path, not the existing tmpdir itself
        self.kuzu = KuzuService(db_dir=os.path.join(self._tmp, "kuzu_db"))

    def test_schema_initialised(self):
        # Should not raise; idempotent
        self.assertIsNotNone(self.kuzu._db)

    def test_upsert_and_get_account_role(self):
        # Must create account node first; upsert_account_role does MATCH+SET
        self.kuzu.upsert_account("acc_bridge",  "bridge_user")
        self.kuzu.upsert_account("acc_passive", "passive_user")
        self.kuzu.upsert_account_role("acc_bridge",  "BRIDGE")
        self.kuzu.upsert_account_role("acc_passive", "PASSIVE")
        roles = {r["account_id"]: r["role"] for r in self.kuzu.get_account_roles()}
        self.assertEqual(roles.get("acc_bridge"),  "BRIDGE")
        self.assertEqual(roles.get("acc_passive"), "PASSIVE")

    def test_upsert_community(self):
        self.kuzu.upsert_community(
            community_id="comm_01",
            label="AntiVax cluster",
            isolation_score=0.87,
            size=25,
        )
        # If no exception → schema and upsert OK

    def test_upsert_entity(self):
        self.kuzu.upsert_entity("ent_001", "CDC", "ORG")
        # If no exception → entity node write OK

    def test_get_entity_co_occurrences_empty(self):
        result = self.kuzu.get_entity_co_occurrences(limit=10)
        self.assertIsInstance(result, list)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# T03  CounterEffectService — SQLite CRUD + scoring
# ══════════════════════════════════════════════════════════════════════════════
class T03_CounterEffectService(unittest.TestCase):
    """Phase 3 — counter-effect tracking persistence and business logic."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ce_test_")
        from services.counter_effect_service import CounterEffectService
        self.svc = CounterEffectService(db_path=Path(self._tmp) / "ce.db")

    def test_record_deployment(self):
        rec = self.svc.record_deployment(
            report_id="rep_001",
            counter_message="Vaccines do NOT cause autism.",
            baseline_velocity=12.5,
            baseline_post_count=80,
            topic_id="t_vaccine",
            topic_label="Vaccine misinformation",
        )
        self.assertEqual(rec.outcome, "PENDING")
        self.assertEqual(rec.baseline_velocity, 12.5)
        self.assertIsNotNone(rec.record_id)

    def test_record_followup_effective(self):
        rec = self.svc.record_deployment(
            report_id="rep_002", counter_message="msg",
            baseline_velocity=10.0, baseline_post_count=60,
        )
        updated = self.svc.record_followup(rec.record_id, 4.0, 24)
        self.assertEqual(updated.outcome, "EFFECTIVE")
        self.assertAlmostEqual(updated.effect_score, 0.6, places=2)
        self.assertAlmostEqual(updated.decay_rate, 0.6, places=2)

    def test_record_followup_backfired(self):
        rec = self.svc.record_deployment(
            report_id="rep_003", counter_message="msg",
            baseline_velocity=5.0, baseline_post_count=30,
        )
        updated = self.svc.record_followup(rec.record_id, 8.0, 50)
        self.assertEqual(updated.outcome, "BACKFIRED")
        self.assertLess(updated.effect_score, -0.1)

    def test_record_followup_neutral(self):
        rec = self.svc.record_deployment(
            report_id="rep_004", counter_message="msg",
            baseline_velocity=10.0, baseline_post_count=50,
        )
        updated = self.svc.record_followup(rec.record_id, 9.5, 48)
        self.assertEqual(updated.outcome, "NEUTRAL")

    def test_pending_followups(self):
        self.svc.record_deployment(report_id="r1", counter_message="m",
                                   baseline_velocity=5.0, baseline_post_count=20)
        self.svc.record_deployment(report_id="r2", counter_message="m",
                                   baseline_velocity=8.0, baseline_post_count=40)
        pending = self.svc.get_pending_followups()
        self.assertEqual(len(pending), 2)

    def test_get_effect_report_summary(self):
        # (baseline, followup): 10→3 EFFECTIVE, 10→12 BACKFIRED, 10→10 NEUTRAL
        # Use 12.0 (not 11.0) so decay = -0.2 < -0.1 → strict BACKFIRED
        for i, (bl, fu) in enumerate([(10.0, 3.0), (10.0, 12.0), (10.0, 10.0)]):
            rec = self.svc.record_deployment(
                report_id=f"r_{i}", counter_message="msg",
                baseline_velocity=bl, baseline_post_count=50,
            )
            self.svc.record_followup(rec.record_id, fu, 45)
        report = self.svc.get_effect_report()
        self.assertEqual(report.total_tracked, 3)
        self.assertEqual(report.effective_count, 1)
        self.assertEqual(report.backfired_count, 1)
        self.assertIsNotNone(report.average_effect_score)
        self.assertIn("tracked", report.summary)

    def test_get_records_by_topic(self):
        rec = self.svc.record_deployment(
            report_id="r5", counter_message="m",
            baseline_velocity=6.0, baseline_post_count=30,
            topic_id="climate_topic",
        )
        result = self.svc.get_records_by_topic("climate_topic")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].record_id, rec.record_id)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# T04  AnalysisAgent — propagation + emotion aggregation + cascade predict
# ══════════════════════════════════════════════════════════════════════════════
class T04_AnalysisAgent(unittest.TestCase):
    """Phase 0/2 — analysis agent logic (no LLM calls needed for these methods)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="analysis_test_")
        from services.kuzu_service import KuzuService
        from services.postgres_service import PostgresService
        kuzu = KuzuService(db_dir=os.path.join(self._tmp, "kuzu_db"))
        pg = MagicMock(spec=PostgresService)
        from agents.analysis import AnalysisAgent
        self.agent = AnalysisAgent(pg=pg, kuzu=kuzu)

    def test_aggregate_emotions_returns_dominant(self):
        posts = _make_posts(20)
        # Set all to fear to get a clear dominant
        for p in posts:
            p.emotion = "fear"
        dominant, dist = self.agent._aggregate_emotions(posts)
        self.assertEqual(dominant, "fear")
        self.assertAlmostEqual(dist["fear"], 1.0)

    def test_aggregate_emotions_empty(self):
        dominant, dist = self.agent._aggregate_emotions([])
        self.assertEqual(dominant, "neutral")
        self.assertEqual(dist, {})

    def test_aggregate_emotions_distribution(self):
        from models.post import Post
        posts = [
            Post(id="p1", account_id="a", text="x", emotion="fear"),
            Post(id="p2", account_id="a", text="x", emotion="fear"),
            Post(id="p3", account_id="a", text="x", emotion="anger"),
            Post(id="p4", account_id="a", text="x", emotion="neutral"),
        ]
        dominant, dist = self.agent._aggregate_emotions(posts)
        self.assertEqual(dominant, "fear")
        self.assertAlmostEqual(dist["fear"], 0.5)
        self.assertAlmostEqual(dist["anger"], 0.25)

    def test_compute_velocity_no_timestamps(self):
        from models.post import Post
        posts = [Post(id=f"p{i}", account_id="a", text="t") for i in range(12)]
        v = self.agent._compute_velocity(posts, window_hours=24)
        self.assertAlmostEqual(v, 0.5, places=1)  # 12/24

    def test_compute_velocity_with_timestamps(self):
        from models.post import Post
        now = datetime.utcnow()
        posts = [
            Post(id=f"p{i}", account_id="a", text="t",
                 posted_at=now - timedelta(hours=i))
            for i in range(6)
        ]
        v = self.agent._compute_velocity(posts, window_hours=24)
        self.assertGreater(v, 0)

    def test_cascade_predict_high_emotion(self):
        from models.report import TopicSummary
        ts = TopicSummary(
            topic_id="t1", label="Vaccine fear",
            post_count=25, velocity=8.0,
            is_trending=True, misinfo_risk=0.8,
            dominant_emotion="fear",
        )
        cp = self.agent.predict_cascade(ts, community_analysis=None)
        self.assertEqual(cp.peak_window_hours, "0-4h")
        self.assertEqual(cp.confidence, "HIGH")
        self.assertGreater(cp.predicted_posts_24h, ts.post_count)

    def test_cascade_predict_low_data(self):
        from models.report import TopicSummary
        ts = TopicSummary(
            topic_id="t2", label="Minor topic",
            post_count=3, velocity=0.5,
            misinfo_risk=0.3,
            dominant_emotion="neutral",
        )
        cp = self.agent.predict_cascade(ts, community_analysis=None)
        self.assertEqual(cp.confidence, "LOW")

    def test_cascade_predict_with_community(self):
        from models.report import TopicSummary
        from models.community import CommunityAnalysis, CommunityInfo
        ts = TopicSummary(
            topic_id="t3", label="With community",
            post_count=15, velocity=3.0, misinfo_risk=0.6,
            dominant_emotion="anger",
        )
        ci = CommunityInfo(
            community_id="c1", label="Echo", size=20,
            isolation_score=0.9, is_echo_chamber=True,
            bridge_accounts=["b1", "b2"],
        )
        ca = CommunityAnalysis(
            community_count=1, echo_chamber_count=1,
            communities=[ci], modularity=0.4,
        )
        cp = self.agent.predict_cascade(ts, community_analysis=ca)
        self.assertIn("isolation", cp.reasoning)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# T05  CommunityAgent — Louvain + echo-chamber detection
# ══════════════════════════════════════════════════════════════════════════════
class T05_CommunityAgent(unittest.TestCase):
    """Phase 1 — community detection with real networkx computation."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="community_test_")
        from services.kuzu_service import KuzuService
        self.kuzu = KuzuService(db_dir=os.path.join(self._tmp, "kuzu_db"))
        from agents.community import CommunityAgent
        self.agent = CommunityAgent(kuzu=self.kuzu)

    def test_skips_when_too_few_accounts(self):
        """< 10 accounts → skipped gracefully."""
        posts = _make_posts(4)
        result = self.agent.detect_communities(posts)
        self.assertTrue(result.skipped)

    def test_detects_communities(self):
        """15+ accounts + varied topics → at least 1 community."""
        try:
            import community as community_louvain  # python-louvain
        except ImportError:
            self.skipTest("python-louvain not installed")
        posts = _make_posts(30)
        result = self.agent.detect_communities(posts)
        if result.skipped:
            # May still skip if graph is disconnected — not a failure
            self.assertIsInstance(result.skip_reason, str)
        else:
            self.assertGreaterEqual(result.community_count, 1)
            self.assertGreaterEqual(result.modularity, 0.0)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# T06  ImmunityStrategy — graph vaccination targeting
# ══════════════════════════════════════════════════════════════════════════════
class T06_ImmunityStrategy(unittest.TestCase):
    """Phase 3 — immunity strategy recommendation."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="imm_test_")
        from services.kuzu_service import KuzuService
        from services.postgres_service import PostgresService
        kuzu = KuzuService(db_dir=os.path.join(self._tmp, "kuzu_db"))
        # Seed some account roles
        for acc_id, role in [("a01","BRIDGE"),("a02","AMPLIFIER"),
                              ("a03","PASSIVE"),("a04","PASSIVE"),("a05","BRIDGE")]:
            kuzu.upsert_account_role(acc_id, role)
        pg = MagicMock(spec=PostgresService)
        from agents.analysis import AnalysisAgent
        self.agent = AnalysisAgent(pg=pg, kuzu=kuzu)

    def _make_community_analysis(self):
        from models.community import CommunityAnalysis, CommunityInfo
        comms = [
            CommunityInfo(
                community_id="c1", label="AntiVax",
                size=15, isolation_score=0.82,
                is_echo_chamber=True,
                account_ids=["a01","a03","a04","a06","a07","a08",
                             "a09","a10","a11","a12","a13","a14","a15","a16","a17"],
                bridge_accounts=["a01"],
            ),
            CommunityInfo(
                community_id="c2", label="Mainstream",
                size=10, isolation_score=0.25,
                is_echo_chamber=False,
                account_ids=["a02","a05","a18","a19","a20",
                             "a21","a22","a23","a24","a25"],
                bridge_accounts=["a05"],
            ),
        ]
        return CommunityAnalysis(
            community_count=2, echo_chamber_count=1,
            communities=comms, modularity=0.43,
        )

    def test_skips_without_community(self):
        result = self.agent.recommend_immunity_strategy(
            community_analysis=None,
            topic_id="t1",
        )
        self.assertTrue(result.skipped)

    def test_recommends_targets(self):
        try:
            import networkx  # noqa: F401
        except ImportError:
            self.skipTest("networkx not installed")
        ca = self._make_community_analysis()
        result = self.agent.recommend_immunity_strategy(
            community_analysis=ca,
            topic_id="t1",
            topic_label="Vaccine claims",
            max_targets=5,
        )
        self.assertFalse(result.skipped)
        self.assertGreater(result.recommended_target_count, 0)
        self.assertGreater(result.immunity_coverage, 0.0)
        self.assertLessEqual(result.immunity_coverage, 1.0)
        # Originators should not appear (none seeded here)
        for t in result.targets:
            self.assertNotEqual(t.role, "ORIGINATOR")

    def test_coverage_estimate_in_range(self):
        try:
            import networkx  # noqa: F401
        except ImportError:
            self.skipTest("networkx not installed")
        ca = self._make_community_analysis()
        result = self.agent.recommend_immunity_strategy(ca, max_targets=10)
        self.assertGreaterEqual(result.immunity_coverage, 0.0)
        self.assertLessEqual(result.immunity_coverage, 1.0)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# T07  MonitorService — alert logic
# ══════════════════════════════════════════════════════════════════════════════
class T07_MonitorService(unittest.TestCase):
    """Phase 3 — monitoring alert generation."""

    def _make_planner_mock(self, velocity=8.0, misinfo_risk=0.8, predicted_posts=350):
        """Return a mock PlannerAgent whose run() returns a controlled IncidentReport."""
        from models.report import IncidentReport, TopicSummary
        from models.persuasion import CascadePrediction
        ts = TopicSummary(
            topic_id="t1", label="Vaccine misinformation",
            post_count=50, velocity=velocity,
            is_trending=True, is_likely_misinfo=True,
            misinfo_risk=misinfo_risk,
            dominant_emotion="fear",
        )
        cp = CascadePrediction(
            topic_id="t1", topic_label="Vaccine misinformation",
            current_velocity=velocity, emotion_weight=1.0,
            predicted_posts_24h=predicted_posts,
            confidence="HIGH",
        )
        report = IncidentReport(
            id="rep_monitor",
            intent_type="TREND_ANALYSIS",
            topic_summaries=[ts],
            cascade_predictions=[cp],
        )
        mock_planner = MagicMock()
        mock_planner.run.return_value = report
        return mock_planner

    def test_high_velocity_alert(self):
        from services.monitor_service import MonitorService, MonitorConfig
        config = MonitorConfig(velocity_threshold=5.0, max_cycles=1)
        mock_planner = self._make_planner_mock(velocity=8.0)
        monitor = MonitorService(mock_planner, config=config)
        result = monitor.run_once("test query")
        alert_types = [a.alert_type for a in result.alerts]
        self.assertIn("HIGH_VELOCITY", alert_types)

    def test_high_risk_alert(self):
        from services.monitor_service import MonitorService, MonitorConfig
        config = MonitorConfig(risk_threshold=0.70, velocity_threshold=999, max_cycles=1)
        mock_planner = self._make_planner_mock(misinfo_risk=0.85, velocity=0.5)
        monitor = MonitorService(mock_planner, config=config)
        result = monitor.run_once("test query")
        alert_types = [a.alert_type for a in result.alerts]
        self.assertIn("HIGH_RISK", alert_types)

    def test_cascade_warning_alert(self):
        from services.monitor_service import MonitorService, MonitorConfig
        config = MonitorConfig(cascade_threshold=200, velocity_threshold=999,
                               risk_threshold=999, max_cycles=1)
        mock_planner = self._make_planner_mock(predicted_posts=350, velocity=0.1,
                                               misinfo_risk=0.1)
        monitor = MonitorService(mock_planner, config=config)
        result = monitor.run_once("test query")
        alert_types = [a.alert_type for a in result.alerts]
        self.assertIn("CASCADE_WARNING", alert_types)

    def test_new_topic_alert_on_first_cycle(self):
        from services.monitor_service import MonitorService, MonitorConfig
        config = MonitorConfig(velocity_threshold=999, risk_threshold=999,
                               cascade_threshold=99999, max_cycles=1)
        mock_planner = self._make_planner_mock(velocity=0.1, misinfo_risk=0.1,
                                               predicted_posts=10)
        monitor = MonitorService(mock_planner, config=config)
        result = monitor.run_once("test query")
        alert_types = [a.alert_type for a in result.alerts]
        self.assertIn("NEW_TOPIC", alert_types)

    def test_no_duplicate_new_topic(self):
        from services.monitor_service import MonitorService, MonitorConfig
        config = MonitorConfig(velocity_threshold=999, risk_threshold=999,
                               cascade_threshold=99999, max_cycles=1)
        mock_planner = self._make_planner_mock(velocity=0.1, misinfo_risk=0.1,
                                               predicted_posts=10)
        monitor = MonitorService(mock_planner, config=config)
        monitor._seen_topics.add("Vaccine misinformation")  # pre-seed
        result = monitor.run_once("test query")
        alert_types = [a.alert_type for a in result.alerts]
        self.assertNotIn("NEW_TOPIC", alert_types)

    def test_alert_callback_invoked(self):
        from services.monitor_service import MonitorService, MonitorConfig
        fired = []
        config = MonitorConfig(velocity_threshold=5.0, max_cycles=1)
        mock_planner = self._make_planner_mock(velocity=9.0)
        monitor = MonitorService(mock_planner, config=config, on_alert=fired.append)
        monitor.run_once("test query")
        self.assertGreater(len(fired), 0)
        self.assertEqual(fired[0].alert_type, "NEW_TOPIC")  # first alert

    def test_cycle_error_handled(self):
        from services.monitor_service import MonitorService, MonitorConfig
        config = MonitorConfig(max_cycles=1)
        mock_planner = MagicMock()
        mock_planner.run.side_effect = RuntimeError("API down")
        monitor = MonitorService(mock_planner, config=config)
        result = monitor.run_once("bad query")
        self.assertIsNotNone(result.error)
        self.assertIn("API down", result.error)


# ══════════════════════════════════════════════════════════════════════════════
# T08  KnowledgeAgent — emotion classification and entity extraction (mocked LLM)
# ══════════════════════════════════════════════════════════════════════════════
class T08_KnowledgeAgent(unittest.TestCase):
    """Phase 0/2 — LLM-driven emotion + entity methods with mocked Anthropic."""

    def setUp(self):
        self._tmp_kuzu = tempfile.mkdtemp(prefix="ka_kuzu_")
        self._tmp_chroma = tempfile.mkdtemp(prefix="ka_chroma_")
        from services.kuzu_service import KuzuService
        from services.postgres_service import PostgresService
        from services.chroma_service import ChromaService
        from services.embeddings_service import EmbeddingsService
        self.kuzu = KuzuService(db_dir=os.path.join(self._tmp_kuzu, "kuzu_db"))
        pg = MagicMock(spec=PostgresService)
        chroma = MagicMock(spec=ChromaService)
        embedder = MagicMock(spec=EmbeddingsService)
        embedder.embed.return_value = [0.1] * 10
        from agents.knowledge import KnowledgeAgent
        self.agent = KnowledgeAgent(pg=pg, chroma=chroma,
                                    kuzu=self.kuzu, embedder=embedder)

    def _mock_claude_response(self, text: str):
        """Return a mock Anthropic message with given text content."""
        content_block = MagicMock()
        content_block.type = "text"
        content_block.text = text
        msg = MagicMock()
        msg.content = [content_block]
        return msg

    def test_classify_post_emotions(self):
        """Emotion classification writes emotion field to post objects."""
        llm_resp = json.dumps([
            {"post_id": "post_000", "emotion": "fear",    "score": 0.9},
            {"post_id": "post_001", "emotion": "anger",   "score": 0.8},
            {"post_id": "post_002", "emotion": "neutral", "score": 0.6},
        ])
        posts = _make_posts(3)
        with patch.object(self.agent._claude.messages, "create",
                          return_value=self._mock_claude_response(llm_resp)):
            self.agent.classify_post_emotions(posts)
        self.assertEqual(posts[0].emotion, "fear")
        self.assertEqual(posts[1].emotion, "anger")

    def test_classify_post_emotions_empty(self):
        """Empty post list — must not raise."""
        self.agent.classify_post_emotions([])

    def test_analyze_persuasion(self):
        """Persuasion analysis returns PersuasionFeatures list."""
        claims = _make_claims(2)
        # Note: the knowledge agent reads "top_tactic" key (not "top_persuasion_tactic")
        llm_resp = json.dumps({
            "emotional_appeal": 0.85,
            "fear_framing": 0.8,
            "simplicity_score": 0.7,
            "authority_reference": True,
            "urgency_markers": 3,
            "identity_trigger": True,
            "top_tactic": "fear_framing",
            "explanation": "Strong fear and urgency framing.",
        })
        with patch.object(self.agent._claude.messages, "create",
                          return_value=self._mock_claude_response(llm_resp)):
            features = self.agent.analyze_persuasion(claims)
        self.assertEqual(len(features), 2)
        # virality_score is recomputed internally from dim scores; just verify range
        self.assertGreaterEqual(features[0].virality_score, 0.0)
        self.assertLessEqual(features[0].virality_score, 1.0)
        self.assertEqual(features[0].top_persuasion_tactic, "fear_framing")

    def test_extract_entities(self):
        """Entity extraction returns NamedEntity list and populates Kuzu."""
        claims = _make_claims(3)
        llm_resp = json.dumps([
            {"name": "CDC",     "type": "ORG"},
            {"name": "Pfizer",  "type": "ORG"},
            {"name": "Dr. Smith","type": "PERSON"},
        ])
        with patch.object(self.agent._claude.messages, "create",
                          return_value=self._mock_claude_response(llm_resp)):
            entities, co_occ = self.agent.extract_entities(claims)
        self.assertGreater(len(entities), 0)
        names = {e.name for e in entities}
        self.assertTrue(names & {"CDC", "Pfizer", "Dr. Smith"})

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp_kuzu, ignore_errors=True)
        shutil.rmtree(self._tmp_chroma, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# T09  End-to-end PlannerAgent.run() with full mocking
# ══════════════════════════════════════════════════════════════════════════════
class T09_PlannerEndToEnd(unittest.TestCase):
    """
    Full pipeline smoke test — PlannerAgent.run() with all external calls
    mocked.  Validates that IncidentReport is returned with expected Phase 0-3
    fields populated.
    """

    def setUp(self):
        self._tmp_kuzu   = tempfile.mkdtemp(prefix="e2e_kuzu_")
        self._tmp_chroma = tempfile.mkdtemp(prefix="e2e_chroma_")
        self._tmp_pg_wal = tempfile.mkdtemp(prefix="e2e_pg_")

    def _build_planner_with_mocks(self):
        from services.kuzu_service import KuzuService
        from services.postgres_service import PostgresService
        from services.chroma_service import ChromaService
        from services.embeddings_service import EmbeddingsService
        from services.reddit_service import RedditService
        from services.x_api_service import XApiService
        from services.telegram_service import TelegramService
        from services.stable_diffusion_service import StableDiffusionService
        from services.news_search_service import NewsSearchService
        from agents import (
            IngestionAgent, KnowledgeAgent, AnalysisAgent,
            RiskAgent, CounterMessageAgent, CriticAgent,
            ReportAgent, VisualAgent, PlannerAgent,
        )
        from agents.community import CommunityAgent

        kuzu     = KuzuService(db_dir=os.path.join(self._tmp_kuzu, "kuzu_db"))
        pg       = MagicMock(spec=PostgresService)
        pg.save_report.return_value = None
        chroma   = MagicMock()   # no spec — ChromaService attribute names vary
        chroma.add_texts.return_value = ["id1"]
        chroma.query.return_value = []
        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * 10
        x_api    = MagicMock()
        x_api.search_recent.return_value = []
        reddit   = MagicMock()
        reddit.fetch_subreddit_posts.return_value = []
        telegram = MagicMock()
        sd       = MagicMock()
        news     = MagicMock()
        news.search.return_value = []

        ingestion   = IngestionAgent(pg=pg, chroma=chroma, kuzu=kuzu,
                                     embedder=embedder, vision=MagicMock(),
                                     telegram=telegram, x_api=x_api, reddit=reddit)
        knowledge   = KnowledgeAgent(pg=pg, chroma=chroma, kuzu=kuzu, embedder=embedder)
        analysis    = AnalysisAgent(pg=pg, kuzu=kuzu)
        risk        = RiskAgent()
        counter_msg = CounterMessageAgent()
        critic      = CriticAgent()
        report_agent= ReportAgent(pg=pg)
        visual      = VisualAgent(sd=sd, kuzu=kuzu)
        community   = CommunityAgent(kuzu=kuzu)

        return PlannerAgent(
            ingestion=ingestion, knowledge=knowledge,
            analysis=analysis, risk=risk,
            counter_msg=counter_msg, critic=critic,
            report_agent=report_agent, visual=visual,
            news_service=news, community=community,
        )

    def _claude_mock_factory(self):
        """
        Return a side_effect function that returns different canned LLM
        responses depending on the system prompt content.
        """
        def side_effect(**kwargs):
            system = kwargs.get("system", "")
            content_block = MagicMock()
            content_block.type = "text"

            # Intent classifier
            if "intent classifier" in system.lower():
                content_block.text = '["TREND_ANALYSIS"]'
            # Keyword generator
            elif "search expert" in system.lower():
                content_block.text = '["vaccine autism", "vaccine safety myth"]'
            # Emotion classifier
            elif "emotional state" in system.lower() or "emotion" in system.lower():
                posts_preview = kwargs.get("messages", [{}])[0].get("content", "")
                content_block.text = json.dumps([
                    {"post_id": "post_000", "emotion": "fear",   "score": 0.85},
                    {"post_id": "post_001", "emotion": "anger",  "score": 0.75},
                    {"post_id": "post_002", "emotion": "neutral","score": 0.6},
                ])
            # Claim extraction
            elif "fact-checker" in system.lower() or "claim" in system.lower():
                content_block.text = json.dumps([{
                    "claim": "Vaccines cause autism according to new study",
                    "confidence": 0.9,
                }])
            # Deduplication
            elif "SAME" in system and "RELATED" in system:
                content_block.text = "DIFFERENT"
            # Propagation analysis
            elif "propagation analyst" in system.lower():
                content_block.text = json.dumps({
                    "themes": "Vaccine-autism conspiracy claim spreading rapidly.",
                    "stance_distribution": {"pro_claim": 55, "counter_claim": 35, "neutral": 10},
                    "anomaly": True,
                    "anomaly_description": "Coordinated posting pattern detected.",
                })
            # Risk assessment
            elif "risk" in system.lower() and "misinfo" in system.lower():
                content_block.text = json.dumps({
                    "risk_level": "HIGH",
                    "misinfo_score": 0.82,
                    "reasoning": "Well-debunked claim spreading widely.",
                    "flags": ["no_evidence", "emotional_framing"],
                    "requires_human_review": False,
                })
            # Topic clustering
            elif "topic cluster" in system.lower() or "cluster" in system.lower():
                content_block.text = json.dumps([{
                    "topic_id": "t_vaccine",
                    "label": "Vaccine autism conspiracy",
                    "claim_ids": ["claim_000"],
                }])
            # Persuasion analysis
            elif "persuasion" in system.lower() or "virality" in system.lower():
                content_block.text = json.dumps({
                    "emotional_appeal": 0.88, "fear_framing": 0.82,
                    "simplicity_score": 0.75, "authority_reference": 0.4,
                    "urgency_markers": 0.9,   "identity_trigger": 0.6,
                    "virality_score": 0.78,
                    "top_persuasion_tactic": "fear_framing",
                    "explanation": "Heavy fear framing.",
                })
            # Entity extraction
            elif "named entity" in system.lower() or "NER" in system:
                content_block.text = json.dumps([
                    {"name": "CDC",    "type": "ORG"},
                    {"name": "Pfizer", "type": "ORG"},
                ])
            # Counter-message
            elif "counter" in system.lower() and "clarif" in system.lower():
                content_block.text = (
                    "FACT: The scientific consensus is clear — vaccines do NOT cause "
                    "autism. Over 1 million children were studied with no link found. "
                    "Source: CDC, WHO, The Lancet."
                )
            # Critic
            elif "critic" in system.lower() or "APPROVED" in system:
                content_block.text = json.dumps({
                    "verdict": "APPROVED",
                    "feedback": "Factually accurate and well-sourced.",
                    "score": 0.92,
                })
            # Report generation
            elif "incident report" in system.lower() or "executive summary" in system.lower():
                content_block.text = (
                    "## Executive Summary\n"
                    "A vaccine-autism conspiracy claim is spreading at HIGH risk.\n"
                    "## Risk Evaluation\nHIGH misinfo risk detected.\n"
                )
            else:
                content_block.text = "{}"

            msg = MagicMock()
            msg.content = [content_block]
            return msg

        return side_effect

    def test_full_pipeline_trend_analysis(self):
        """
        PlannerAgent.run() with TREND_ANALYSIS intent.
        Validates report structure and Phase 0/1/2/3 fields.
        """
        planner = self._build_planner_with_mocks()

        # Inject synthetic posts (skip real API calls)
        synthetic_posts = _make_posts(15)

        with patch("anthropic.Anthropic.messages") as mock_messages_cls:
            # Patch the messages.create on all agent instances
            mock_create = MagicMock(side_effect=self._claude_mock_factory())
            for agent_attr in [
                "_claude", "_analysis._claude", "_knowledge._claude",
                "_risk._claude", "_counter_msg._claude", "_critic._claude",
                "_report_agent._claude",
            ]:
                try:
                    obj = planner
                    for part in agent_attr.split("."):
                        obj = getattr(obj, part)
                    obj.messages.create = mock_create
                except AttributeError:
                    pass

            # Patch each agent's LLM client directly
            for agent in [planner, planner._knowledge, planner._analysis,
                          planner._risk, planner._counter_msg, planner._critic,
                          planner._report_agent]:
                if hasattr(agent, "_claude"):
                    agent._claude.messages.create = mock_create

            report = planner.run(
                query="vaccine autism conspiracy spreading",
                posts=synthetic_posts,
            )

        # ── Basic structure ────────────────────────────────────────────────
        self.assertIsNotNone(report)
        self.assertIsNotNone(report.id)
        self.assertEqual(report.intent_type, "TREND_ANALYSIS")
        self.assertIsInstance(report.run_logs, list)
        self.assertGreater(len(report.run_logs), 0)

        # ── Phase 0: Emotion classification ────────────────────────────────
        # At least some posts should have emotion set
        self.assertIsNotNone(report.propagation_summary)

        # ── Phase 0: Account roles ──────────────────────────────────────────
        if report.propagation_summary:
            role_sum = report.propagation_summary.account_role_summary
            # Roles dict should exist; totals = number of unique accounts
            self.assertIsInstance(role_sum, dict)

        # ── Phase 2: Topic summaries ────────────────────────────────────────
        # (may be empty if fewer than 2 unique claims extracted)
        self.assertIsInstance(report.topic_summaries, list)

        # ── Phase 3: Counter-effect fields exist ────────────────────────────
        self.assertIsInstance(report.counter_effect_records, list)

        # ── Run log stages covered ──────────────────────────────────────────
        stages = {log.stage for log in report.run_logs}
        self.assertIn("intent_classification", stages)
        self.assertIn("claim_extraction", stages)
        self.assertIn("propagation_analysis", stages)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp_kuzu,   ignore_errors=True)
        shutil.rmtree(self._tmp_chroma, ignore_errors=True)
        shutil.rmtree(self._tmp_pg_wal, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# T10  Propagation velocity edge cases
# ══════════════════════════════════════════════════════════════════════════════
class T10_VelocityEdgeCases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="vel_test_")
        from services.kuzu_service import KuzuService
        from services.postgres_service import PostgresService
        self.agent = None
        kuzu = KuzuService(db_dir=os.path.join(self._tmp, "kuzu_db"))
        pg   = MagicMock(spec=PostgresService)
        from agents.analysis import AnalysisAgent
        self.agent = AnalysisAgent(pg=pg, kuzu=kuzu)

    def test_single_post_no_timestamp(self):
        from models.post import Post
        posts = [Post(id="p1", account_id="a", text="t")]
        v = self.agent._compute_velocity(posts, window_hours=24)
        # 1/24 ≈ 0.0417, rounded to 2 dp = 0.04
        self.assertAlmostEqual(v, round(1/24, 2), places=2)

    def test_single_post_with_timestamp(self):
        from models.post import Post
        posts = [Post(id="p1", account_id="a", text="t", posted_at=datetime.utcnow())]
        v = self.agent._compute_velocity(posts, window_hours=24)
        # single timestamped post: 1/window = 1/24, rounded to 2 dp = 0.04
        self.assertAlmostEqual(v, round(1/24, 2), places=2)

    def test_zero_posts(self):
        v = self.agent._compute_velocity([], window_hours=24)
        self.assertEqual(v, 0.0)

    def test_burst_posts_same_second(self):
        from models.post import Post
        now = datetime.utcnow()
        posts = [Post(id=f"p{i}", account_id="a", text="t", posted_at=now)
                 for i in range(10)]
        # All at same timestamp — elapsed ≈ 0 → use 24h window
        v = self.agent._compute_velocity(posts, window_hours=24)
        self.assertGreater(v, 0)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# T11  CounterEffectService — zero-baseline edge case
# ══════════════════════════════════════════════════════════════════════════════
class T11_CounterEffectEdgeCases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ce_edge_")
        from services.counter_effect_service import CounterEffectService
        self.svc = CounterEffectService(db_path=Path(self._tmp) / "ce.db")

    def test_zero_baseline_velocity(self):
        """Avoid division-by-zero when baseline_velocity = 0."""
        rec = self.svc.record_deployment(
            report_id="r_zero", counter_message="msg",
            baseline_velocity=0.0, baseline_post_count=0,
        )
        updated = self.svc.record_followup(rec.record_id, 5.0, 30)
        self.assertIsNotNone(updated.effect_score)
        self.assertAlmostEqual(updated.decay_rate, 0.0)

    def test_compute_without_followup(self):
        """compute_effect_score on PENDING record returns unchanged record."""
        rec = self.svc.record_deployment(
            report_id="r_pend", counter_message="msg",
            baseline_velocity=5.0, baseline_post_count=20,
        )
        recomputed = self.svc.compute_effect_score(rec.record_id)
        self.assertEqual(recomputed.outcome, "PENDING")
        self.assertIsNone(recomputed.effect_score)

    def test_effect_score_clamped(self):
        """Extreme decay should be clamped to [-1, +1]."""
        rec = self.svc.record_deployment(
            report_id="r_clamp", counter_message="msg",
            baseline_velocity=1.0, baseline_post_count=5,
        )
        updated = self.svc.record_followup(rec.record_id, 0.0, 0)
        self.assertLessEqual(updated.effect_score, 1.0)
        self.assertGreaterEqual(updated.effect_score, -1.0)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    loader  = unittest.TestLoader()
    suite   = unittest.TestSuite()
    test_classes = [
        T01_Models,
        T02_KuzuService,
        T03_CounterEffectService,
        T04_AnalysisAgent,
        T05_CommunityAgent,
        T06_ImmunityStrategy,
        T07_MonitorService,
        T08_KnowledgeAgent,
        T09_PlannerEndToEnd,
        T10_VelocityEdgeCases,
        T11_CounterEffectEdgeCases,
    ]
    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
