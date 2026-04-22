"""
Community Workspace — community-detect skill.

Phase 1 additions:
  Task 2.7  — Community detection & echo-chamber analysis (Louvain)
  Task 1.2  — Trust / influence scoring (PageRank)
  Task 2.8  — Cross-community coordination detection

Algorithm:
  1. Export Account–Topic bipartite graph from Kuzu.
  2. Project to Account–Account unipartite graph (shared topic = edge).
  3. Run Louvain community detection (python-louvain).
  4. Compute isolation score per community (intra / total edge ratio).
  5. Compute PageRank influence scores per account.
  6. Detect cross-community coordination via claim similarity.
  7. Persist results to Kuzu Community nodes.

Graceful degradation:
  - If networkx / python-louvain are not installed → skips with a warning.
  - If < MIN_ACCOUNTS accounts → skips (insufficient graph data).
"""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Optional

import structlog

from models.community import (
    CommunityAnalysis, CommunityInfo, CoordinationSignal, EchoChamberScore
)
from models.post import Post

if TYPE_CHECKING:
    from services.kuzu_service import KuzuService

log = structlog.get_logger(__name__)

# Minimum number of accounts to attempt community detection
MIN_ACCOUNTS = 10
# Isolation score above which a community is flagged as an echo chamber
ECHO_CHAMBER_THRESHOLD = 0.75


class CommunityAgent:
    """
    Social network analysis agent.
    Requires: networkx, python-louvain (community package).
    """

    def __init__(self, kuzu: "KuzuService") -> None:
        self._kuzu = kuzu

    # ── Skill: community-detect ────────────────────────────────────────────────

    def detect_communities(
        self,
        all_posts: Optional[list[Post]] = None,
    ) -> CommunityAnalysis:
        """
        Main entry point.  Builds the social graph from Kuzu, runs Louvain,
        computes echo-chamber scores, persists results, returns CommunityAnalysis.
        """
        try:
            import networkx as nx
        except ImportError:
            log.warning("community.networkx_missing")
            return CommunityAnalysis(
                skipped=True,
                skip_reason="networkx not installed — run: pip install networkx",
            )

        try:
            import community as community_louvain  # python-louvain
        except ImportError:
            log.warning("community.louvain_missing")
            return CommunityAnalysis(
                skipped=True,
                skip_reason="python-louvain not installed — run: pip install python-louvain",
            )

        # ── Build bipartite graph Account–Topic ───────────────────────────
        edges = self._kuzu.get_account_topic_edges()
        accounts = self._kuzu.get_all_accounts()

        if len(accounts) < MIN_ACCOUNTS:
            log.info("community.insufficient_data", account_count=len(accounts))
            return CommunityAnalysis(
                skipped=True,
                skip_reason=f"Only {len(accounts)} accounts — minimum {MIN_ACCOUNTS} required",
            )

        # Build unipartite Account–Account projection:
        # edge weight = number of shared topics
        topic_accounts: dict[str, list[str]] = defaultdict(list)
        for e in edges:
            topic_accounts[e["topic_id"]].append(e["account_id"])

        G = nx.Graph()
        for acc in accounts:
            G.add_node(acc["account_id"])
        for topic_id, accs in topic_accounts.items():
            for i in range(len(accs)):
                for j in range(i + 1, len(accs)):
                    a, b = accs[i], accs[j]
                    if G.has_edge(a, b):
                        G[a][b]["weight"] = G[a][b].get("weight", 1) + 1
                    else:
                        G.add_edge(a, b, weight=1)

        if G.number_of_nodes() < MIN_ACCOUNTS:
            return CommunityAnalysis(
                skipped=True,
                skip_reason="Insufficient connected accounts for community detection",
            )

        # ── Louvain community detection ────────────────────────────────────
        partition: dict[str, int] = community_louvain.best_partition(
            G, weight="weight", random_state=42
        )
        modularity = community_louvain.modularity(partition, G, weight="weight")
        log.info("community.louvain_done",
                 communities=len(set(partition.values())),
                 modularity=round(modularity, 3))

        # ── PageRank influence scores ──────────────────────────────────────
        pagerank: dict[str, float] = nx.pagerank(G, weight="weight")

        # ── Group accounts by community ────────────────────────────────────
        comm_accounts: dict[int, list[str]] = defaultdict(list)
        for account_id, comm_id in partition.items():
            comm_accounts[comm_id].append(account_id)

        # ── Build account→topics lookup for bridge detection ──────────────
        acc_topics: dict[str, set[str]] = defaultdict(set)
        for e in edges:
            acc_topics[e["account_id"]].add(e["topic_id"])

        # ── Per-community analysis ─────────────────────────────────────────
        community_infos: list[CommunityInfo] = []

        for comm_id, member_ids in comm_accounts.items():
            # Intra / total edges → isolation score
            intra = sum(
                1 for u, v in G.edges(member_ids)
                if partition.get(u) == comm_id and partition.get(v) == comm_id
            )
            total = G.degree(member_ids[0]) if member_ids else 1
            total_edges = sum(G.degree(a) for a in member_ids) // 2
            isolation = (intra / max(total_edges, 1)) if total_edges > 0 else 0.0

            # Dominant topics (most common across members)
            topic_counter: Counter = Counter()
            for acc in member_ids:
                for tid in acc_topics.get(acc, set()):
                    topic_counter[tid] += 1
            dominant_topic_ids = [t for t, _ in topic_counter.most_common(3)]

            # Dominant emotion (from post objects if available)
            dominant_emotion = self._community_dominant_emotion(
                member_ids, all_posts or []
            )

            # Bridge accounts: high PageRank members who also appear in other communities
            bridge_ids = [
                acc for acc in member_ids
                if (pagerank.get(acc, 0) > 0.01
                    and any(
                        partition.get(nb) != comm_id
                        for nb in G.neighbors(acc)
                    ))
            ][:5]

            is_echo = isolation >= ECHO_CHAMBER_THRESHOLD

            cinfo = CommunityInfo(
                community_id=str(comm_id),
                label=f"Community-{comm_id}",
                size=len(member_ids),
                isolation_score=round(isolation, 3),
                dominant_topics=dominant_topic_ids,
                dominant_emotion=dominant_emotion,
                is_echo_chamber=is_echo,
                account_ids=member_ids,
                bridge_accounts=bridge_ids,
            )
            community_infos.append(cinfo)

            # Persist to Kuzu
            kuzu_id = f"community-{comm_id}"
            self._kuzu.upsert_community(
                kuzu_id,
                cinfo.label,
                isolation_score=isolation,
                size=cinfo.size,
            )
            for acc in member_ids:
                self._kuzu.add_belongs_to_community(acc, kuzu_id)

        # Sort: most isolated / largest first
        community_infos.sort(
            key=lambda c: (c.is_echo_chamber, c.isolation_score, c.size),
            reverse=True,
        )

        # ── Cross-community coordination detection ─────────────────────────
        coord_signals = self._detect_cross_community_coordination(
            community_infos, G, partition
        )

        # Persist coordination edges
        for sig in coord_signals:
            self._kuzu.add_coordinated_with(sig.account_a, sig.account_b)

        echo_count = sum(1 for c in community_infos if c.is_echo_chamber)
        log.info(
            "community.analysis_done",
            total_communities=len(community_infos),
            echo_chambers=echo_count,
            modularity=round(modularity, 3),
            coord_signals=len(coord_signals),
        )
        return CommunityAnalysis(
            community_count=len(community_infos),
            echo_chamber_count=echo_count,
            communities=community_infos,
            cross_community_signals=coord_signals,
            modularity=round(modularity, 3),
        )

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _community_dominant_emotion(
        member_ids: list[str],
        all_posts: list[Post],
    ) -> str:
        """Return the most common emotion among a community's posts."""
        _VALID = {"fear", "anger", "hope", "disgust", "neutral"}
        member_set = set(member_ids)
        counts: Counter = Counter()
        for p in all_posts:
            if p.account_id in member_set and p.emotion in _VALID:
                counts[p.emotion] += 1
        if not counts:
            return "neutral"
        return counts.most_common(1)[0][0]

    def _detect_cross_community_coordination(
        self,
        communities: list[CommunityInfo],
        G,
        partition: dict[str, int],
    ) -> list[CoordinationSignal]:
        """
        Detect cross-community coordination: pairs of accounts from different
        communities sharing 2+ identical claims (already computed via Kuzu).
        """
        coord_rows = self._kuzu.get_coordinated_accounts(min_shared_claims=2)
        signals: list[CoordinationSignal] = []

        # Build account → community lookup
        acc_comm: dict[str, str] = {
            acc: c.community_id
            for c in communities
            for acc in c.account_ids
        }

        for row in coord_rows[:20]:  # cap
            a1, a2 = row["account1"], row["account2"]
            c1, c2 = acc_comm.get(a1), acc_comm.get(a2)
            if c1 and c2 and c1 != c2:
                signals.append(CoordinationSignal(
                    account_a=a1,
                    community_a=c1,
                    account_b=a2,
                    community_b=c2,
                    shared_claim_count=row["shared_claim_count"],
                    sample_claims=row.get("sample_claims", []),
                ))

        return signals
