"""Phase 3 tests — decision / visual tools + Visual / Compare / ExplainDecision.

Visual generation is network-heavy (SD). Tests patch the VisualAgent so no
real rendering happens.
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

def _claim_obj(
    claim_id: str,
    text: str,
    *,
    supporting: int = 0,
    contradicting: int = 0,
) -> dict:
    mk = lambda stance, i: {
        "article_id": f"{claim_id}-{stance}-{i}",
        "article_title": f"t{i}",
        "article_url": f"u{i}",
        "source_name": "Reuters",
        "stance": stance,
        "snippet": "s",
        "source_tier": "internal_chroma",
    }
    return {
        "id": claim_id,
        "normalized_text": text,
        "supporting_evidence": [mk("supports", i) for i in range(supporting)],
        "contradicting_evidence": [mk("contradicts", i) for i in range(contradicting)],
        "uncertain_evidence": [],
        "claim_actionability": "actionable",
        "non_actionable_reason": None,
    }


def _raw_with_decision(
    decision: str,
    *,
    primary_id: str = "c1",
    counter: str = "",
    visual_card_path: str = "",
    supporting: int = 0,
    contradicting: int = 2,
) -> dict:
    return {
        "id": "r",
        "intent_type": "TREND_ANALYSIS",
        "claims": [
            _claim_obj(
                primary_id,
                "Vaccines cause autism",
                supporting=supporting,
                contradicting=contradicting,
            )
        ],
        "topic_summaries": [],
        "intervention_decision": {
            "primary_claim_id": primary_id,
            "primary_claim_text": "Vaccines cause autism",
            "decision": decision,
            "explanation": f"test {decision}",
            "reason": "context_sparse",
            "recommended_next_step": "monitor",
            "visual_type": (
                "rebuttal_card" if decision == "rebut"
                else "evidence_context_card" if decision == "evidence_context"
                else None
            ),
        },
        "counter_message": counter,
        "visual_card_path": visual_card_path,
        "propagation_summary": {
            "post_count": 10, "unique_accounts": 7, "velocity": 1.0,
        },
    }


def _build_run(
    root: Path, run_id: str, raw: dict,
    *, metrics: dict | None = None,
) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps({
            "run_id": run_id,
            "post_count": raw.get("propagation_summary", {}).get("post_count", 0),
            "query_text": f"q-{run_id}",
        }),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics or {
            "evidence_coverage": 0.5,
            "community_modularity_q": 0.4,
        }),
        encoding="utf-8",
    )
    (run_dir / "report_raw.json").write_text(json.dumps(raw), encoding="utf-8")
    return run_dir


def _clear_caches():
    import tools.graph_tools as gt
    gt._cached_raw.cache_clear()


# ─── T1: decision_tools ──────────────────────────────────────────────────────

class T1_DecisionTools(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="dec_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run(
            self._runs, "20260421-010000-aaaaaa",
            _raw_with_decision("rebut", counter="Actually, vaccines are safe."),
        )
        import tools.run_query_tools as m
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
        ]
        for p in self._patches:
            p.start()
        _clear_caches()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_get_intervention_decision(self):
        from tools.decision_tools import (
            get_intervention_decision, GetInterventionDecisionInput,
        )
        out = get_intervention_decision(GetInterventionDecisionInput(run_id="latest"))
        self.assertIsNotNone(out.decision)
        self.assertEqual(out.decision.decision, "rebut")
        self.assertEqual(out.counter_message, "Actually, vaccines are safe.")

    def test_counter_effect_history_requires_identifier(self):
        from tools.decision_tools import (
            get_counter_effect_history, GetCounterEffectHistoryInput,
        )
        from tools.base import ToolInputError
        with self.assertRaises(ToolInputError):
            get_counter_effect_history(GetCounterEffectHistoryInput())


# ─── T2: visual_tools (patched agent) ────────────────────────────────────────

class T2_VisualTools(unittest.TestCase):
    def test_clarification_card_returns_path(self):
        from tools import visual_tools
        fake_agent = MagicMock()
        fake_agent.generate_clarification_card.return_value = "/tmp/card.png"
        with patch.object(visual_tools, "_get_visual_agent", return_value=fake_agent):
            out = visual_tools.generate_clarification_card(
                visual_tools.GenerateClarificationCardInput(
                    counter_message="Actually X.",
                    claim=visual_tools.ClaimPayload(
                        id="c1", normalized_text="A bad claim",
                    ),
                )
            )
        self.assertEqual(out.path, "/tmp/card.png")
        self.assertIsNone(out.reason)

    def test_evidence_context_card_none_path(self):
        from tools import visual_tools
        fake_agent = MagicMock()
        fake_agent.generate_evidence_context_card.return_value = None
        with patch.object(visual_tools, "_get_visual_agent", return_value=fake_agent):
            out = visual_tools.generate_evidence_context_card(
                visual_tools.GenerateEvidenceContextCardInput(
                    claim=visual_tools.ClaimPayload(
                        id="c1", normalized_text="x",
                    ),
                )
            )
        self.assertIsNone(out.path)
        self.assertEqual(out.reason, "no_supporting_evidence")


# ─── T3: VisualSummaryCapability branching ───────────────────────────────────

class T3_VisualSummary(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="vsum_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
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

    def _setup(self, run_id: str, raw: dict):
        _build_run(self._runs, run_id, raw)
        _clear_caches()

    def test_abstain_returns_no_image(self):
        self._setup("20260421-010000-abstain", _raw_with_decision("abstain"))
        from capabilities.visual_summary_capability import (
            VisualSummaryCapability, VisualSummaryInput,
        )
        cap = VisualSummaryCapability()
        out = cap.run(VisualSummaryInput(run_id="20260421-010000-abstain"))
        self.assertEqual(out.status, "abstained")
        self.assertIsNone(out.image_path)

    def test_rebut_renders_via_visual_tools(self):
        self._setup(
            "20260421-010000-rebut",
            _raw_with_decision("rebut", counter="Actually X."),
        )
        from capabilities import visual_summary_capability
        from tools import visual_tools
        fake_agent = MagicMock()
        fake_agent.generate_clarification_card.return_value = "/tmp/c.png"
        with patch.object(visual_tools, "_get_visual_agent", return_value=fake_agent):
            cap = visual_summary_capability.VisualSummaryCapability()
            out = cap.run(
                visual_summary_capability.VisualSummaryInput(
                    run_id="20260421-010000-rebut"
                )
            )
        self.assertEqual(out.status, "rendered")
        self.assertEqual(out.image_path, "/tmp/c.png")

    def test_rebut_without_counter_is_insufficient(self):
        self._setup(
            "20260421-010000-nocounter",
            _raw_with_decision("rebut", counter=""),
        )
        from capabilities.visual_summary_capability import (
            VisualSummaryCapability, VisualSummaryInput,
        )
        cap = VisualSummaryCapability()
        out = cap.run(VisualSummaryInput(run_id="20260421-010000-nocounter"))
        self.assertEqual(out.status, "insufficient_data")

    def test_reuses_precomputed_card(self):
        self._setup(
            "20260421-010000-cached",
            _raw_with_decision(
                "rebut", counter="x", visual_card_path="/existing/c.png"
            ),
        )
        from capabilities.visual_summary_capability import (
            VisualSummaryCapability, VisualSummaryInput,
        )
        cap = VisualSummaryCapability()
        out = cap.run(VisualSummaryInput(run_id="20260421-010000-cached"))
        self.assertEqual(out.status, "rendered")
        self.assertEqual(out.image_path, "/existing/c.png")


# ─── T4: RunCompareCapability ────────────────────────────────────────────────

class T4_RunCompare(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="cmp_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run(
            self._runs, "20260421-020000-newer",
            _raw_with_decision("abstain"),
            metrics={"evidence_coverage": 0.7, "community_modularity_q": 0.5},
        )
        _build_run(
            self._runs, "20260421-010000-older",
            {
                **_raw_with_decision("abstain"),
                "propagation_summary": {
                    "post_count": 5, "unique_accounts": 3, "velocity": 0.5,
                },
            },
            metrics={"evidence_coverage": 0.4, "community_modularity_q": 0.3},
        )
        import tools.run_query_tools as m
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
        ]
        for p in self._patches:
            p.start()
        _clear_caches()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_compare_latest_picks_baseline(self):
        from capabilities.run_compare_capability import (
            RunCompareCapability, RunCompareInput,
        )
        cap = RunCompareCapability()
        out = cap.run(RunCompareInput(run_id="latest"))
        self.assertEqual(out.target_run_id, "20260421-020000-newer")
        self.assertEqual(out.baseline_run_id, "20260421-010000-older")
        # evidence_coverage should be in changes and direction="up"
        ec = next(c for c in out.changes if c.field == "evidence_coverage")
        self.assertEqual(ec.direction, "up")
        self.assertAlmostEqual(ec.delta, 0.3)
        pc = next(c for c in out.changes if c.field == "post_count")
        self.assertEqual(pc.direction, "up")

    def test_missing_baseline_raises(self):
        # Fresh temp with only one run
        with tempfile.TemporaryDirectory() as tmp:
            solo = Path(tmp) / "runs"
            solo.mkdir()
            samples = Path(tmp) / "samples"
            samples.mkdir()
            _build_run(solo, "solo", _raw_with_decision("abstain"))
            import tools.run_query_tools as m
            import tools.graph_tools as gt
            with (
                patch.object(m, "_DATA_ROOT", solo),
                patch.object(m, "_SAMPLE_ROOT", samples),
            ):
                gt._cached_raw.cache_clear()
                from capabilities.base import CapabilityError
                from capabilities.run_compare_capability import (
                    RunCompareCapability, RunCompareInput,
                )
                cap = RunCompareCapability()
                with self.assertRaises(CapabilityError):
                    cap.run(RunCompareInput(run_id="latest"))


# ─── T5: ExplainDecisionCapability ───────────────────────────────────────────

class T5_ExplainDecision(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="expl_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run(
            self._runs, "20260421-010000-explain",
            _raw_with_decision("rebut", counter="Actually X."),
        )
        import tools.run_query_tools as m
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
        ]
        for p in self._patches:
            p.start()
        _clear_caches()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_explain_returns_decision(self):
        from capabilities.explain_decision_capability import (
            ExplainDecisionCapability, ExplainDecisionInput,
        )
        cap = ExplainDecisionCapability()
        out = cap.run(ExplainDecisionInput(run_id="latest"))
        self.assertIsNotNone(out.decision)
        self.assertEqual(out.decision.decision, "rebut")
        self.assertEqual(out.counter_message, "Actually X.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
