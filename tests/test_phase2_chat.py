"""Phase 2 tests — claim / evidence / graph tools + two new capabilities.

Strategy:
  - Fake `report_raw.json` with structured claims + community_analysis +
    propagation_summary to exercise the new tool paths.
  - Evidence tools are not tested against Chroma (network); they're tested
    by patching the lazy service getters.
  - Capabilities run against the temp tree with no LLM dependency.
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

def _claim(
    claim_id: str,
    text: str,
    *,
    supporting: int = 0,
    contradicting: int = 0,
    uncertain: int = 0,
    actionability: str = "actionable",
    reason: str | None = None,
) -> dict:
    mk = lambda stance, i: {
        "article_id": f"{claim_id}-{stance}-{i}",
        "article_title": f"{stance} article {i}",
        "article_url": f"https://example.com/{claim_id}/{stance}/{i}",
        "source_name": "Reuters",
        "stance": stance,
        "snippet": f"{stance} snippet {i}",
        "source_tier": "internal_chroma",
    }
    return {
        "id": claim_id,
        "normalized_text": text,
        "supporting_evidence": [mk("supports", i) for i in range(supporting)],
        "contradicting_evidence": [mk("contradicts", i) for i in range(contradicting)],
        "uncertain_evidence": [mk("neutral", i) for i in range(uncertain)],
        "claim_actionability": actionability,
        "non_actionable_reason": reason,
    }


def _fake_raw() -> dict:
    return {
        "id": "report-x",
        "intent_type": "TREND_ANALYSIS",
        "query_text": "test",
        "claims": [
            _claim("c1", "Vaccines cause autism", contradicting=2, uncertain=1),
            _claim("c2", "The sky is blue", supporting=3),
            _claim("c3", "This is just my opinion",
                   actionability="non_actionable", reason="non_factual_expression"),
        ],
        "topic_summaries": [],
        "intervention_decision": {
            "primary_claim_id": "c1",
            "visual_type": "clarification",
        },
        "propagation_summary": {
            "post_count": 42,
            "unique_accounts": 30,
            "velocity": 5.0,
            "coordinated_pairs": 2,
            "bridge_influence_ratio": 0.25,
            "account_role_summary": {
                "ORIGINATOR": 2, "AMPLIFIER": 8, "BRIDGE": 3, "PASSIVE": 17,
            },
            "anomaly_detected": False,
        },
        "community_analysis": {
            "community_count": 3,
            "echo_chamber_count": 1,
            "modularity": 0.42,
            "communities": [
                {
                    "community_id": "comm-1", "label": "Skeptics", "size": 12,
                    "isolation_score": 0.9, "is_echo_chamber": True,
                    "dominant_emotion": "anger",
                    "dominant_topics": ["t-a"],
                    "bridge_accounts": ["@bridge1"],
                },
                {
                    "community_id": "comm-2", "label": "Mainstream", "size": 8,
                    "isolation_score": 0.3, "is_echo_chamber": False,
                    "dominant_emotion": "neutral",
                    "dominant_topics": ["t-a", "t-b"],
                    "bridge_accounts": [],
                },
            ],
            "cross_community_signals": [],
        },
    }


def _build_run_dir(root: Path, run_id: str) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"run_id": run_id, "post_count": 42}), encoding="utf-8"
    )
    (run_dir / "metrics.json").write_text(
        json.dumps({"bridge_influence_ratio": 0.25, "evidence_coverage": 0.7}),
        encoding="utf-8",
    )
    (run_dir / "report_raw.json").write_text(
        json.dumps(_fake_raw()), encoding="utf-8"
    )
    return run_dir


# ─── T1: Claim-related tools ─────────────────────────────────────────────────

class T1_ClaimTools(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="claim_tools_"))
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
        # clear graph_tools lru_cache between tests
        import tools.graph_tools as gt
        gt._cached_raw.cache_clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_get_claims_returns_counts(self):
        from tools.run_query_tools import get_claims, GetClaimsInput
        out = get_claims(GetClaimsInput(run_id="latest"))
        self.assertEqual(len(out.claims), 3)
        c1 = next(c for c in out.claims if c.claim_id == "c1")
        self.assertEqual(c1.contradicting_count, 2)
        self.assertEqual(c1.uncertain_count, 1)
        self.assertEqual(out.primary_claim_id, "c1")

    def test_get_claim_details_found(self):
        from tools.run_query_tools import (
            get_claim_details, GetClaimDetailsInput,
        )
        out = get_claim_details(
            GetClaimDetailsInput(run_id="latest", claim_id="c2")
        )
        self.assertEqual(out.claim.claim_id, "c2")
        self.assertEqual(len(out.claim.supporting_evidence), 3)

    def test_get_claim_details_missing_raises(self):
        from tools.run_query_tools import (
            get_claim_details, GetClaimDetailsInput,
        )
        from tools.base import ToolInputError
        with self.assertRaises(ToolInputError):
            get_claim_details(
                GetClaimDetailsInput(run_id="latest", claim_id="missing")
            )

    def test_get_primary_claim_found(self):
        from tools.run_query_tools import (
            get_primary_claim, GetPrimaryClaimInput,
        )
        out = get_primary_claim(GetPrimaryClaimInput(run_id="latest"))
        self.assertEqual(out.primary_claim_id, "c1")
        self.assertIsNotNone(out.claim)
        self.assertEqual(out.claim.claim_id, "c1")


# ─── T2: Graph tools ─────────────────────────────────────────────────────────

class T2_GraphTools(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="graph_tools_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run_dir(self._runs, "20260421-010000-aaaaaa")
        import tools.run_query_tools as m
        import tools.graph_tools as gt
        # Force the JSON fallback path: this test class writes a synthetic
        # report_raw.json fixture but never seeds Kuzu. In production the two
        # backends are coherent because both are populated by the same
        # pipeline run.
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
            patch.object(gt, "_kuzu_or_none", lambda: None),
        ]
        for p in self._patches:
            p.start()
        gt._cached_raw.cache_clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_query_topic_graph_all_communities(self):
        from tools.graph_tools import query_topic_graph, QueryTopicGraphInput
        out = query_topic_graph(QueryTopicGraphInput(run_id="latest"))
        self.assertEqual(out.community_count, 3)
        self.assertEqual(out.echo_chamber_count, 1)
        self.assertEqual(len(out.communities), 2)
        # Sorted by size desc
        self.assertEqual(out.communities[0].community_id, "comm-1")

    def test_query_topic_graph_topic_filter(self):
        from tools.graph_tools import query_topic_graph, QueryTopicGraphInput
        out = query_topic_graph(
            QueryTopicGraphInput(run_id="latest", topic_id="t-b")
        )
        # Only comm-2 has t-b in dominant_topics
        self.assertEqual(len(out.communities), 1)
        self.assertEqual(out.communities[0].community_id, "comm-2")

    def test_get_social_metrics(self):
        from tools.graph_tools import get_social_metrics, GetSocialMetricsInput
        out = get_social_metrics(GetSocialMetricsInput(run_id="latest"))
        self.assertEqual(out.metrics["bridge_influence_ratio"], 0.25)

    def test_get_propagation_summary(self):
        from tools.graph_tools import (
            get_propagation_summary, GetPropagationSummaryInput,
        )
        out = get_propagation_summary(GetPropagationSummaryInput(run_id="latest"))
        self.assertEqual(out.propagation_summary["post_count"], 42)


# ─── T3: Evidence tools (network-free) ───────────────────────────────────────

class T3_EvidenceTools(unittest.TestCase):
    def test_retrieve_evidence_chunks_filters_by_similarity(self):
        from tools import evidence_tools
        fake_embedder = MagicMock()
        fake_embedder.embed.return_value = [0.1] * 5
        fake_chroma = MagicMock()
        fake_chroma.query_articles.return_value = [
            {
                "id": "a1::0",
                "document": "text one",
                "metadata": {
                    "article_id": "a1", "title": "A1",
                    "url": "u1", "source": "Reuters",
                },
                "distance": 0.2,  # similarity 0.8 — keep
            },
            {
                "id": "a2::0",
                "document": "text two",
                "metadata": {"article_id": "a2"},
                "distance": 0.9,  # similarity 0.1 — drop
            },
        ]
        with (
            patch.object(evidence_tools, "_get_embedder", return_value=fake_embedder),
            patch.object(evidence_tools, "_get_chroma", return_value=fake_chroma),
        ):
            out = evidence_tools.retrieve_evidence_chunks(
                evidence_tools.RetrieveEvidenceChunksInput(
                    query_text="q", min_similarity=0.5,
                )
            )
        self.assertEqual(len(out.chunks), 1)
        self.assertEqual(out.chunks[0].article_id, "a1")

    def test_retrieve_official_sources_combines_wiki_and_news(self):
        from tools import evidence_tools
        fake_wiki = MagicMock()
        fake_wiki.fetch_summary.return_value = {
            "article_id": "wiki:X", "title": "X", "url": "u",
            "snippet": "s", "source_name": "Wikipedia",
        }
        fake_news = MagicMock()
        fake_news.search_and_fetch.return_value = [
            {
                "article_id": "n1", "title": "N1", "url": "nu",
                "body": "nbody", "source": "BBC", "published": "",
            }
        ]
        with (
            patch.object(evidence_tools, "_get_wikipedia", return_value=fake_wiki),
            patch.object(evidence_tools, "_get_news", return_value=fake_news),
        ):
            out = evidence_tools.retrieve_official_sources(
                evidence_tools.RetrieveOfficialSourcesInput(
                    query_text="claim text", news_max_results=1,
                )
            )
        tiers = sorted(s.tier for s in out.sources)
        self.assertEqual(tiers, ["news", "wikipedia"])


# ─── T4: ClaimStatusCapability verdict ladder ────────────────────────────────

class T4_ClaimStatusCapability(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="cap_cs_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run_dir(self._runs, "20260421-010000-aaaaaa")
        import tools.run_query_tools as m
        import tools.graph_tools as gt
        # Force the JSON fallback path: this test class writes a synthetic
        # report_raw.json fixture but never seeds Kuzu. In production the two
        # backends are coherent because both are populated by the same
        # pipeline run.
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
            patch.object(gt, "_kuzu_or_none", lambda: None),
        ]
        for p in self._patches:
            p.start()
        gt._cached_raw.cache_clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_primary_claim_is_contradicted(self):
        from capabilities.claim_status_capability import (
            ClaimStatusCapability, ClaimStatusInput,
        )
        cap = ClaimStatusCapability()
        out = cap.run(ClaimStatusInput(run_id="latest"))
        self.assertEqual(out.claim_id, "c1")
        self.assertEqual(out.verdict_label, "contradicted")
        self.assertEqual(out.contradicting_count, 2)

    def test_supported_verdict(self):
        from capabilities.claim_status_capability import (
            ClaimStatusCapability, ClaimStatusInput,
        )
        cap = ClaimStatusCapability()
        out = cap.run(ClaimStatusInput(run_id="latest", claim_id="c2"))
        self.assertEqual(out.verdict_label, "supported")
        self.assertEqual(out.supporting_count, 3)

    def test_non_factual_verdict(self):
        from capabilities.claim_status_capability import (
            ClaimStatusCapability, ClaimStatusInput,
        )
        cap = ClaimStatusCapability()
        out = cap.run(ClaimStatusInput(run_id="latest", claim_id="c3"))
        self.assertEqual(out.verdict_label, "non_factual")


# ─── T5: PropagationInsightCapability ────────────────────────────────────────

class T5_PropagationCapability(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="cap_prop_"))
        self._runs = self._tmp / "runs"
        self._samples = self._tmp / "sample_runs"
        self._runs.mkdir()
        self._samples.mkdir()
        _build_run_dir(self._runs, "20260421-010000-aaaaaa")
        import tools.run_query_tools as m
        import tools.graph_tools as gt
        # Force the JSON fallback path: this test class writes a synthetic
        # report_raw.json fixture but never seeds Kuzu. In production the two
        # backends are coherent because both are populated by the same
        # pipeline run.
        self._patches = [
            patch.object(m, "_DATA_ROOT", self._runs),
            patch.object(m, "_SAMPLE_ROOT", self._samples),
            patch.object(gt, "_kuzu_or_none", lambda: None),
        ]
        for p in self._patches:
            p.start()
        gt._cached_raw.cache_clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_propagation_insight_aggregates(self):
        from capabilities.propagation_insight_capability import (
            PropagationInsightCapability, PropagationInsightInput,
        )
        cap = PropagationInsightCapability()
        out = cap.run(PropagationInsightInput(run_id="latest"))
        self.assertEqual(out.post_count, 42)
        self.assertEqual(out.unique_accounts, 30)
        self.assertEqual(out.coordinated_pairs, 2)
        self.assertEqual(out.community_count, 3)
        self.assertEqual(out.echo_chamber_count, 1)
        self.assertAlmostEqual(out.bridge_influence_ratio, 0.25)
        self.assertEqual(out.account_role_summary.get("BRIDGE"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
