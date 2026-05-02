"""
KG Query tools - redesign-2026-05 Phase 3.4.

Branch C (Knowledge Graph Query). Replaces the v1 NetworkX-on-JSON path.
Pure Kuzu Cypher; NetworkX only used as a degraded analytics fallback when
Kuzu is empty (community / centrality computed on the in-memory graph).

Four query kinds:
    1. propagation_path   - shortest path / k-hop reachability between accounts
    2. key_nodes          - PageRank / degree centrality over a topic subgraph
    3. community_relations - (account, account) co-posting + cross-community bridges
    4. topic_correlation  - shared-entity strength between two topics

The legacy `tools/graph_tools.py` still serves the v1 capabilities path; we
keep it untouched and ship the v2 entry points under a new module name
(`kg_query_tools`) so the v2 chat link can pick them up cleanly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import structlog

from models.branch_output import KGEdge, KGNode, KGOutput
from services.kuzu_service import KuzuService

log = structlog.get_logger(__name__)


@dataclass
class KGQueryTool:
    kuzu: Optional[KuzuService] = None

    def __post_init__(self) -> None:
        if self.kuzu is None:
            try:
                self.kuzu = KuzuService()
            except Exception as exc:
                log.warning("kg.kuzu_unavailable", error=str(exc)[:120])
                self.kuzu = None

    # ── Public entry points ───────────────────────────────────────────────────

    def propagation_path(
        self, source_account: str, target_account: str,
        max_hops: int = 4,
    ) -> KGOutput:
        t0 = time.monotonic()
        out = KGOutput(query_kind="propagation_path",
                       target={"source": source_account,
                                "target": target_account,
                                "max_hops": max_hops})
        if not self.kuzu:
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out
        rows = self.kuzu._safe_execute(  # type: ignore[attr-defined]
            "MATCH p = (a:Account {id: $aid})-[:Posted|Replied*1.." + str(max_hops) +
            "]->(b:Account {id: $bid}) "
            "RETURN nodes(p) AS nodes_, rels(p) AS rels_ LIMIT 5",
            {"aid": source_account, "bid": target_account},
        ) or []
        for row in rows:
            for n in (row.get("nodes_") or []):
                nid = (n.get("id") if isinstance(n, dict)
                       else getattr(n, "id", str(n)))
                out.nodes.append(KGNode(id=str(nid), label="Account",
                                        properties={}))
        out.metrics["paths_found"] = len(rows)
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out

    def key_nodes(
        self, topic_id: str, top_k: int = 10,
    ) -> KGOutput:
        t0 = time.monotonic()
        out = KGOutput(query_kind="key_nodes",
                       target={"topic_id": topic_id, "top_k": top_k})
        if not self.kuzu:
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        rows = self.kuzu._safe_execute(  # type: ignore[attr-defined]
            "MATCH (a:Account)-[:Posted]->(p:Post)-[:BelongsToTopic]->(t:Topic {id: $tid}) "
            "RETURN a.id AS account_id, a.username AS username, "
            "count(p) AS post_count "
            "ORDER BY post_count DESC LIMIT $k",
            {"tid": topic_id, "k": top_k},
        ) or []
        for r in rows:
            out.nodes.append(KGNode(
                id=r["account_id"], label="Account",
                properties={"username": r.get("username", ""),
                             "post_count": r.get("post_count", 0)},
            ))
        out.metrics["accounts_in_topic"] = len(rows)
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out

    def community_relations(
        self, topic_id: str, min_shared_posts: int = 2,
    ) -> KGOutput:
        t0 = time.monotonic()
        out = KGOutput(
            query_kind="community_relations",
            target={"topic_id": topic_id,
                     "min_shared_posts": min_shared_posts},
        )
        if not self.kuzu:
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        # account pairs that posted in the same topic
        rows = self.kuzu._safe_execute(  # type: ignore[attr-defined]
            "MATCH (a1:Account)-[:Posted]->(p1:Post)-[:BelongsToTopic]->"
            "(t:Topic {id: $tid})<-[:BelongsToTopic]-(p2:Post)<-[:Posted]-(a2:Account) "
            "WHERE a1.id < a2.id "
            "RETURN a1.id AS a, a2.id AS b, count(*) AS shared",
            {"tid": topic_id},
        ) or []
        seen_nodes: set[str] = set()
        for r in rows:
            shared = int(r.get("shared", 0))
            if shared < min_shared_posts:
                continue
            for nid in (r["a"], r["b"]):
                if nid not in seen_nodes:
                    seen_nodes.add(nid)
                    out.nodes.append(KGNode(id=nid, label="Account"))
            out.edges.append(KGEdge(
                source_id=r["a"], target_id=r["b"], rel_type="CoPosted",
                properties={"shared_posts": shared},
            ))
        out.metrics["pair_count"] = len(out.edges)
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out

    def topic_correlation(
        self, topic_a: str, topic_b: str,
    ) -> KGOutput:
        t0 = time.monotonic()
        out = KGOutput(
            query_kind="topic_correlation",
            target={"topic_a": topic_a, "topic_b": topic_b},
        )
        if not self.kuzu:
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        rows = self.kuzu._safe_execute(  # type: ignore[attr-defined]
            "MATCH (p1:Post)-[:BelongsToTopic]->(t1:Topic {id: $a}), "
            "(p1)-[:HasEntity]->(e:Entity), "
            "(p2:Post)-[:HasEntity]->(e), "
            "(p2)-[:BelongsToTopic]->(t2:Topic {id: $b}) "
            "RETURN DISTINCT e.id AS entity_id, e.name AS name, "
            "e.entity_type AS entity_type",
            {"a": topic_a, "b": topic_b},
        ) or []
        for r in rows:
            out.nodes.append(KGNode(
                id=r["entity_id"], label="Entity",
                properties={"name": r.get("name", ""),
                             "entity_type": r.get("entity_type", "")},
            ))
        out.metrics["shared_entity_count"] = len(out.nodes)
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out
