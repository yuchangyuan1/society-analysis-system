"""
KG Analytics tests - redesign-2026-05-kg Phase B.

Smoke-tests the four NetworkX-backed analytics with mocked Kuzu rows.
No real Kuzu / database connection.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.kg_analytics import KGAnalytics
from services.kg_cache import SUBGRAPH_CACHE, bump_write_seq


@pytest.fixture(autouse=True)
def _wipe_cache():
    SUBGRAPH_CACHE.clear()
    bump_write_seq()
    yield
    SUBGRAPH_CACHE.clear()


def _kuzu_with_replies(rows: list[dict]) -> MagicMock:
    kuzu = MagicMock()
    kuzu._safe_execute.return_value = rows
    return kuzu


# ── influencer_rank ──────────────────────────────────────────────────────────

def test_influencer_rank_picks_central_node():
    """
    Fan-in star: bob, carol, dave, eve all reply to alice's posts.
    No dangling sinks elsewhere. PageRank should rank alice highest.
    """
    rows = [
        {"child_account": "bob",   "parent_account": "alice", "weight": 2},
        {"child_account": "carol", "parent_account": "alice", "weight": 2},
        {"child_account": "dave",  "parent_account": "alice", "weight": 2},
        {"child_account": "eve",   "parent_account": "alice", "weight": 2},
        # mutual replies between peripherals to keep the graph live
        {"child_account": "bob",   "parent_account": "carol", "weight": 1},
        {"child_account": "carol", "parent_account": "bob",   "weight": 1},
    ]
    out = KGAnalytics(kuzu=_kuzu_with_replies(rows)).influencer_rank(
        topic_id=None, top_k=3,
    )
    assert out.query_kind == "influencer_rank"
    assert out.nodes
    top = out.nodes[0]
    assert top.id == "alice"
    assert top.properties["pagerank"] > 0


def test_influencer_rank_returns_empty_on_empty_graph():
    out = KGAnalytics(kuzu=_kuzu_with_replies([])).influencer_rank()
    assert out.nodes == []
    assert out.metrics["node_count"] == 0


# ── bridge_accounts ──────────────────────────────────────────────────────────

def test_bridge_accounts_finds_articulation_node():
    """
    Two clusters (a,b,c) and (e,f,g) only connected via 'bridge'.
    Betweenness centrality should rank 'bridge' highest.
    """
    rows = [
        {"child_account": "a", "parent_account": "b", "weight": 1},
        {"child_account": "b", "parent_account": "c", "weight": 1},
        {"child_account": "c", "parent_account": "bridge", "weight": 1},
        {"child_account": "bridge", "parent_account": "e", "weight": 1},
        {"child_account": "e", "parent_account": "f", "weight": 1},
        {"child_account": "f", "parent_account": "g", "weight": 1},
    ]
    out = KGAnalytics(kuzu=_kuzu_with_replies(rows)).bridge_accounts(top_k=3)
    assert out.query_kind == "bridge_accounts"
    top = out.nodes[0]
    assert top.id == "bridge"


def test_bridge_accounts_skips_too_small_graph():
    rows = [{"child_account": "a", "parent_account": "b", "weight": 1}]
    out = KGAnalytics(kuzu=_kuzu_with_replies(rows)).bridge_accounts()
    assert out.metrics.get("reason") == "graph_too_small"


# ── coordinated_groups ───────────────────────────────────────────────────────

def test_coordinated_groups_separates_two_clusters():
    """
    Two cliques (a,b,c) and (x,y,z) with weak inter-cluster edge.
    Louvain should find ≥ 2 communities of size ≥ 3.
    """
    rows: list[dict] = []
    # Cluster 1: a-b-c fully connected (each pair both directions)
    for u, v in [("a", "b"), ("b", "a"), ("a", "c"), ("c", "a"),
                 ("b", "c"), ("c", "b")]:
        rows.append({"child_account": u, "parent_account": v, "weight": 5})
    # Cluster 2: x-y-z fully connected
    for u, v in [("x", "y"), ("y", "x"), ("x", "z"), ("z", "x"),
                 ("y", "z"), ("z", "y")]:
        rows.append({"child_account": u, "parent_account": v, "weight": 5})
    # weak bridge
    rows.append({"child_account": "c", "parent_account": "x", "weight": 1})

    out = KGAnalytics(kuzu=_kuzu_with_replies(rows)).coordinated_groups(
        topic_id=None, min_size=3,
    )
    assert out.query_kind == "coordinated_groups"
    assert out.metrics["community_count"] >= 2
    # All nodes should be assigned to some community
    assigned = {n.id for n in out.nodes}
    assert {"a", "b", "c", "x", "y", "z"} <= assigned


# ── echo_chamber ─────────────────────────────────────────────────────────────

def test_echo_chamber_high_modularity_when_segregated():
    """
    Two tight reply clusters with no cross-cluster edges -> high modularity.
    """
    rows: list[dict] = []
    # Cluster 1
    for u, v in [("p1", "p2"), ("p3", "p2"), ("p4", "p1")]:
        rows.append({"child": u, "parent": v})
    # Cluster 2 (disjoint)
    for u, v in [("q1", "q2"), ("q3", "q2"), ("q4", "q1")]:
        rows.append({"child": u, "parent": v})

    kuzu = MagicMock()
    kuzu._safe_execute.return_value = rows

    out = KGAnalytics(kuzu=kuzu).echo_chamber(
        topic_id="topic_x", modularity_threshold=0.3,
    )
    assert out.query_kind == "echo_chamber"
    assert out.metrics["is_echo_chamber"] is True
    assert out.metrics["modularity"] >= 0.3
    assert out.metrics["community_count"] >= 2


def test_echo_chamber_returns_safe_default_when_too_small():
    out = KGAnalytics(
        kuzu=_kuzu_with_replies([])
    ).echo_chamber(topic_id="topic_x")
    assert out.metrics["is_echo_chamber"] is False


# ── Cache ────────────────────────────────────────────────────────────────────

def test_subgraph_cache_hit_on_repeat():
    rows = [{"child_account": "a", "parent_account": "b", "weight": 1}]
    kuzu = MagicMock()
    kuzu._safe_execute.return_value = rows

    analytics = KGAnalytics(kuzu=kuzu)
    analytics.influencer_rank(topic_id="t1", top_k=5)
    # Second call same topic -> should hit cache, NOT call _safe_execute again
    analytics.influencer_rank(topic_id="t1", top_k=5)
    assert kuzu._safe_execute.call_count == 1


def test_subgraph_cache_invalidates_on_bump():
    rows = [{"child_account": "a", "parent_account": "b", "weight": 1}]
    kuzu = MagicMock()
    kuzu._safe_execute.return_value = rows
    analytics = KGAnalytics(kuzu=kuzu)
    analytics.influencer_rank(topic_id="t1")
    bump_write_seq()
    analytics.influencer_rank(topic_id="t1")
    assert kuzu._safe_execute.call_count == 2
