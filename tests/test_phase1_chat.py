"""Phase 1 tests — run_query tools + Topic / Emotion capabilities + orchestrator.

Strategy:
  - Tools are tested against a temp `data/runs/<id>/` tree built in setUp.
  - LLM calls (emotion interpretation, answer composer) are patched.
  - Router is patched to force deterministic intent classification.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ─── Fixtures ────────────────────────────────────────────────────────────────

def _fake_raw(run_id: str) -> dict:
    return {
        "id": f"report-{run_id}",
        "intent_type": "TREND_ANALYSIS",
        "query_text": f"test query {run_id}",
        "claims": [],  # Phase 0 type is list[Claim]; empty is fine
        "topic_summaries": [
            {
                "topic_id": "t-a",
                "label": "Topic A — vaccines",
                "claim_count": 3,
                "post_count": 10,
                "velocity": 4.2,
                "is_trending": True,
                "misinfo_risk": 0.7,
                "is_likely_misinfo": True,
                "representative_claims": ["Vaccine causes X"],
                "risk_flags": ["high_risk"],
                "dominant_emotion": "fear",
                "emotion_distribution": {"fear": 0.7, "anger": 0.2, "neutral": 0.1},
            },
            {
                "topic_id": "t-b",
                "label": "Topic B — politics",
                "claim_count": 2,
                "post_count": 3,
                "velocity": 1.1,
                "is_trending": False,
                "misinfo_risk": 0.3,
                "is_likely_misinfo": False,
                "representative_claims": ["Politician said Y"],
                "risk_flags": [],
                "dominant_emotion": "anger",
                "emotion_distribution": {"anger": 0.6, "neutral": 0.4},
            },
        ],
    }


def _fake_manifest(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "started_at": "2026-04-21T00:00:00Z",
        "finished_at": "2026-04-21T00:01:00Z",
        "query_text": f"test query {run_id}",
        "subreddits": ["test"],
        "openai_model": "gpt-4o",
        "git_sha": "abcdef",
        "post_count": 13,
        "report_id": f"report-{run_id}",
    }


def _fake_metrics() -> dict:
    return {
        "evidence_coverage": 0.4,
        "community_modularity_q": 0.5,
        "counter_effect_closed_loop_rate": 0.0,
    }


def _build_run_dir(root: Path, run_id: str) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(_fake_manifest(run_id)), encoding="utf-8"
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(_fake_metrics()), encoding="utf-8"
    )
    (run_dir / "report_raw.json").write_text(
        json.dumps(_fake_raw(run_id)), encoding="utf-8"
    )
    (run_dir / "report.md").write_text("# report", encoding="utf-8")
    return run_dir


# ─── T1: run_query_tools against a fresh runs tree ───────────────────────────

class T1_RunQueryTools(unittest.TestCase):
    """Exercise list_runs / get_run_summary / get_topics against temp dir."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="rq_test_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run_dir(self._runs, "20260421-010000-aaaaaa")
        _build_run_dir(self._runs, "20260421-020000-bbbbbb")
        _build_run_dir(self._samples, "sample_one")
        # Patch module-level constants
        import tools.run_query_tools as m
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_list_runs_merges_data_and_samples(self):
        from tools.run_query_tools import list_runs, ListRunsInput
        out = list_runs(ListRunsInput())
        ids = [r.run_id for r in out.runs]
        sources = [r.source for r in out.runs]
        self.assertIn("20260421-020000-bbbbbb", ids)
        self.assertIn("sample_one", ids)
        self.assertIn("data", sources)
        self.assertIn("sample", sources)

    def test_list_runs_excludes_samples(self):
        from tools.run_query_tools import list_runs, ListRunsInput
        out = list_runs(ListRunsInput(include_samples=False))
        for r in out.runs:
            self.assertEqual(r.source, "data")

    def test_get_run_summary_latest(self):
        from tools.run_query_tools import get_run_summary, GetRunSummaryInput
        out = get_run_summary(GetRunSummaryInput(run_id="latest"))
        # Sorting reverse-alpha on dir names returns bbbbbb first.
        self.assertEqual(out.run_id, "20260421-020000-bbbbbb")
        self.assertEqual(out.source, "data")
        self.assertEqual(out.manifest["post_count"], 13)

    def test_get_run_summary_unknown_raises(self):
        from tools.run_query_tools import get_run_summary, GetRunSummaryInput
        from tools.base import ToolInputError
        with self.assertRaises(ToolInputError):
            get_run_summary(GetRunSummaryInput(run_id="does-not-exist"))

    def test_get_topics_sorted_and_truncated(self):
        from tools.run_query_tools import get_topics, GetTopicsInput
        out = get_topics(
            GetTopicsInput(run_id="latest", top_k=1, sort_by="post_count")
        )
        self.assertEqual(len(out.topics), 1)
        self.assertEqual(out.topics[0].topic_id, "t-a")  # post_count=10 wins

    def test_get_topics_sort_by_misinfo_risk(self):
        from tools.run_query_tools import get_topics, GetTopicsInput
        out = get_topics(
            GetTopicsInput(run_id="latest", top_k=2, sort_by="misinfo_risk")
        )
        self.assertEqual(out.topics[0].topic_id, "t-a")  # risk=0.7 > 0.3


# ─── T2: Capabilities ────────────────────────────────────────────────────────

class T2_TopicOverviewCapability(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="cap_to_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run_dir(self._runs, "20260421-010000-aaaaaa")
        import tools.run_query_tools as m
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_topic_overview_returns_structured_topics(self):
        from capabilities.topic_overview_capability import (
            TopicOverviewCapability, TopicOverviewInput,
        )
        cap = TopicOverviewCapability()
        out = cap.run(TopicOverviewInput(run_id="latest", top_k=1))
        self.assertEqual(out.run_id, "20260421-010000-aaaaaa")
        self.assertEqual(out.source, "data")
        self.assertEqual(len(out.topics), 1)
        self.assertTrue(out.topics[0].is_trending)


class T3_EmotionInsightCapability(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="cap_emo_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run_dir(self._runs, "20260421-010000-aaaaaa")
        import tools.run_query_tools as m
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
            # Patch _interpret to avoid hitting OpenAI during tests.
            patch(
                "capabilities.emotion_insight_capability._interpret",
                return_value="(test interpretation)",
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_emotion_aggregates_weighted(self):
        from capabilities.emotion_insight_capability import (
            EmotionInsightCapability, EmotionInsightInput,
        )
        cap = EmotionInsightCapability()
        out = cap.run(EmotionInsightInput(run_id="latest"))
        self.assertEqual(out.run_id, "20260421-010000-aaaaaa")
        # Fear should dominate after weighting (10 posts fear vs 3 posts anger)
        self.assertEqual(out.dominant_emotion, "fear")
        self.assertEqual(out.interpretation, "(test interpretation)")

    def test_emotion_topic_filter(self):
        from capabilities.emotion_insight_capability import (
            EmotionInsightCapability, EmotionInsightInput,
        )
        cap = EmotionInsightCapability()
        out = cap.run(EmotionInsightInput(run_id="latest", topic_id="t-b"))
        # Only topic B (anger-dominant) should remain.
        self.assertEqual(out.dominant_emotion, "anger")
        self.assertEqual(len(out.topic_emotions), 1)


# ─── T4: Router + Orchestrator end-to-end ────────────────────────────────────

class T4_Orchestrator(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="orch_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._sessions = self._tmp / "sessions"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run_dir(self._runs, "20260421-010000-aaaaaa")
        import tools.run_query_tools as m_tools
        import services.session_store as m_ss
        self._patches = [
            patch.object(m_tools, "_DATA_ROOT", self._runs),
            patch.object(m_tools, "_SAMPLE_ROOT", self._samples),
            patch.object(m_ss, "_SESSIONS_DIR", self._sessions),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_orchestrator_routes_to_topic_overview(self):
        from agents.chat_orchestrator import ChatOrchestrator
        from agents.router import RouterOutput, RouterTargets

        mock_router = MagicMock()
        mock_router.classify.return_value = RouterOutput(
            intent="topic_overview",
            targets=RouterTargets(run_id="latest"),
            confidence=0.95,
        )
        mock_composer = MagicMock()
        mock_composer.compose.return_value = "Top topics: Topic A."

        orch = ChatOrchestrator(router=mock_router, composer=mock_composer)
        resp = orch.handle(session_id="test-session", message="what's trending?")

        self.assertEqual(resp.capability_used, "topic_overview")
        self.assertEqual(resp.answer_text, "Top topics: Topic A.")
        self.assertTrue(resp.capability_output)
        self.assertIn("topics", resp.capability_output)

    def test_orchestrator_handles_other_intent(self):
        from agents.chat_orchestrator import ChatOrchestrator
        from agents.router import RouterOutput

        mock_router = MagicMock()
        mock_router.classify.return_value = RouterOutput(intent="other", confidence=0.3)
        mock_composer = MagicMock()
        mock_composer.compose.return_value = "Try asking about topics."

        orch = ChatOrchestrator(router=mock_router, composer=mock_composer)
        resp = orch.handle(session_id="s2", message="tell me a joke")

        self.assertIsNone(resp.capability_used)
        self.assertIn("topics", resp.answer_text.lower())

    def test_session_state_persists_across_turns(self):
        from agents.chat_orchestrator import ChatOrchestrator
        from agents.router import RouterOutput, RouterTargets
        from services import session_store

        mock_router = MagicMock()
        mock_router.classify.return_value = RouterOutput(
            intent="topic_overview",
            targets=RouterTargets(run_id="latest"),
            confidence=0.9,
        )
        mock_composer = MagicMock(compose=MagicMock(return_value="ok"))

        orch = ChatOrchestrator(router=mock_router, composer=mock_composer)
        orch.handle(session_id="persist-me", message="topics?")

        state = session_store.load("persist-me")
        self.assertEqual(len(state.conversation), 2)  # user + assistant
        self.assertEqual(state.current_run_id, "20260421-010000-aaaaaa")


if __name__ == "__main__":
    unittest.main(verbosity=2)
