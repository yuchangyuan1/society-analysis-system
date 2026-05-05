"""
KG analytics - redesign-2026-05-kg Phase B.3.

Four graph-algorithm queries that Kuzu Cypher can't express natively:

    influencer_rank(topic_id)    - PageRank over reply graph
    bridge_accounts()            - betweenness centrality over global graph
    coordinated_groups(topic)    - Louvain community detection on co-reply
    echo_chamber(topic)          - within-community reply density

Pipeline per query:
    1. Pull subgraph from Kuzu (via KuzuService.execute) - cached LRU
    2. Build a NetworkX DiGraph
    3. Run the algorithm
    4. Project the result back into KGOutput (nodes + edges + metrics)

The Kuzu graph stays the canonical store; NetworkX is in-memory only,
no persistence. NetworkX 3.x and python-louvain are required (Phase B.2).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from models.branch_output import KGEdge, KGNode, KGOutput
from services.kg_cache import SUBGRAPH_CACHE
from services.kuzu_service import KuzuService

log = structlog.get_logger(__name__)


# echo_chamber output caps: keep node/edge lists small enough to render in
# the UI graph and stay under the report-writer payload budget. The full
# graph size is still reported via metrics["analyzed_*"].
_ECHO_TOP_COMMUNITIES = 8
_ECHO_NODE_CAP = 120
_ECHO_EDGE_CAP = 240


# ── Subgraph extraction ──────────────────────────────────────────────────────

def _account_reply_graph(
    kuzu: KuzuService,
    topic_id: Optional[str] = None,
    since_days: Optional[int] = None,
) -> tuple[Any, dict]:
    """Build a NetworkX DiGraph where nodes are Account ids and an edge
    A -> B means an account A wrote a Post that replied to a Post by B.

    `since_days` filters the underlying Posts to the last N days using
    `posts_v2.posted_at` (PostgreSQL is source of truth for posted_at;
    Kuzu doesn't store the timestamp, so we resolve the post id whitelist
    via PG and pass it as a Cypher param).

    Returns (graph, metadata). When networkx is unavailable, returns
    (None, {"reason": ...}).
    """
    try:
        import networkx as nx  # type: ignore
    except ImportError:
        return None, {"reason": "networkx_missing"}

    cache_key = ("account_reply_graph", topic_id or "ALL", since_days or 0)
    cached = SUBGRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached, {"cache_hit": True}

    # Optional posted_at filter: resolve the eligible post id set in PG
    eligible_post_ids: Optional[list[str]] = None
    if since_days and since_days > 0:
        try:
            from services.postgres_service import PostgresService
            pg = PostgresService(); pg.connect()
            with pg.cursor() as cur:
                cur.execute(
                    "SELECT post_id FROM posts_v2 "
                    "WHERE posted_at >= NOW() - (%s || ' days')::interval",
                    (str(since_days),),
                )
                eligible_post_ids = [r["post_id"] for r in (cur.fetchall() or [])]
        except Exception as exc:
            log.warning("kg_analytics.freshness_resolve_failed",
                        error=str(exc)[:160])
            eligible_post_ids = None

    if topic_id:
        if eligible_post_ids is not None:
            cypher = (
                "MATCH (ac:Account)-[:Posted]->(c:Post)-[:Replied]->(p:Post)"
                "<-[:Posted]-(ap:Account), "
                "      (c)-[:BelongsToTopic]->(t:Topic {id: $tid}) "
                "WHERE c.id IN $pids "
                "RETURN ac.id AS child_account, ap.id AS parent_account, "
                "       count(*) AS weight"
            )
            rows = kuzu._safe_execute(  # type: ignore[attr-defined]
                cypher, {"tid": topic_id, "pids": eligible_post_ids},
            ) or []
        else:
            cypher = (
                "MATCH (ac:Account)-[:Posted]->(c:Post)-[:Replied]->(p:Post)"
                "<-[:Posted]-(ap:Account), "
                "      (c)-[:BelongsToTopic]->(t:Topic {id: $tid}) "
                "RETURN ac.id AS child_account, ap.id AS parent_account, "
                "       count(*) AS weight"
            )
            rows = kuzu._safe_execute(cypher, {"tid": topic_id}) or []  # type: ignore[attr-defined]
    else:
        if eligible_post_ids is not None:
            cypher = (
                "MATCH (ac:Account)-[:Posted]->(c:Post)-[:Replied]->(p:Post)"
                "<-[:Posted]-(ap:Account) "
                "WHERE c.id IN $pids "
                "RETURN ac.id AS child_account, ap.id AS parent_account, "
                "       count(*) AS weight"
            )
            rows = kuzu._safe_execute(  # type: ignore[attr-defined]
                cypher, {"pids": eligible_post_ids},
            ) or []
        else:
            cypher = (
                "MATCH (ac:Account)-[:Posted]->(c:Post)-[:Replied]->(p:Post)"
                "<-[:Posted]-(ap:Account) "
                "RETURN ac.id AS child_account, ap.id AS parent_account, "
                "       count(*) AS weight"
            )
            rows = kuzu._safe_execute(cypher) or []  # type: ignore[attr-defined]

    g = nx.DiGraph()
    for r in rows:
        c = r.get("child_account")
        p = r.get("parent_account")
        if not c or not p or c == p:
            continue
        g.add_edge(str(c), str(p), weight=int(r.get("weight", 1)))

    SUBGRAPH_CACHE.put(cache_key, g)
    return g, {
        "cache_hit": False,
        "analyzed_edge_count": g.number_of_edges(),
        "since_days": since_days,
    }


def _topic_post_reply_graph(
    kuzu: KuzuService, topic_id: str,
) -> Any:
    """Reply graph at the Post level inside one topic. Used by echo chamber
    detection where we need within-topic structural density."""
    try:
        import networkx as nx  # type: ignore
    except ImportError:
        return None
    cache_key = ("topic_post_reply", topic_id)
    cached = SUBGRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    cypher = (
        "MATCH (c:Post)-[:Replied]->(p:Post), "
        "      (c)-[:BelongsToTopic]->(t:Topic {id: $tid}) "
        "RETURN c.id AS child, p.id AS parent"
    )
    rows = kuzu._safe_execute(cypher, {"tid": topic_id}) or []  # type: ignore[attr-defined]
    g = nx.DiGraph()
    for r in rows:
        if r.get("child") and r.get("parent"):
            g.add_edge(str(r["child"]), str(r["parent"]))
    SUBGRAPH_CACHE.put(cache_key, g)
    return g


def _append_influencer_context_edges(
    out: KGOutput,
    g: Any,
    scores: dict[str, float],
    top_accounts: set[str],
    *,
    max_nodes: int = 40,
    max_edges: int = 80,
) -> None:
    """Add a compact one-hop subgraph around ranked influencer accounts."""
    seen_nodes = {node.id for node in out.nodes}

    def _add_node(account_id: str, role: str) -> bool:
        if account_id in seen_nodes:
            return True
        if len(out.nodes) >= max_nodes:
            return False
        seen_nodes.add(account_id)
        out.nodes.append(KGNode(
            id=account_id,
            label="Account",
            properties={
                "pagerank": round(float(scores.get(account_id, 0.0)), 6),
                "in_degree": int(g.in_degree(account_id)),
                "out_degree": int(g.out_degree(account_id)),
                "role": role,
            },
        ))
        return True

    edge_rows = []
    for u, v, data in g.edges(data=True):
        u = str(u)
        v = str(v)
        if u not in top_accounts and v not in top_accounts:
            continue
        weight = int(data.get("weight", 1) or 1)
        both_top = int(u in top_accounts and v in top_accounts)
        edge_rows.append((both_top, weight, u, v))
    edge_rows.sort(reverse=True)

    seen_edges: set[tuple[str, str]] = set()
    for _both_top, weight, source, target in edge_rows:
        if len(out.edges) >= max_edges:
            break
        if not _add_node(source, "top" if source in top_accounts else "neighbor"):
            continue
        if not _add_node(target, "top" if target in top_accounts else "neighbor"):
            continue
        key = (source, target)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        out.edges.append(KGEdge(
            source_id=source,
            target_id=target,
            rel_type="RepliedTo",
            properties={"weight": weight},
        ))


# ── Public API ──────────────────────────────────────────────────────────────

@dataclass
class KGAnalytics:
    kuzu: Optional[KuzuService] = None

    def __post_init__(self) -> None:
        if self.kuzu is None:
            try:
                self.kuzu = KuzuService()
            except Exception as exc:
                log.warning("kg_analytics.kuzu_unavailable",
                            error=str(exc)[:120])
                self.kuzu = None

    # ── influencer_rank ──────────────────────────────────────────────────────

    def influencer_rank(
        self, topic_id: Optional[str] = None, top_k: int = 10,
        since_days: Optional[int] = 30,
    ) -> KGOutput:
        """PageRank over the account-level reply graph.

        Higher rank = more accounts (transitively) reply to this account's
        posts. Replaces the naive `key_nodes` post_count ordering.
        `since_days` defaults to 30 days (Day 7 freshness rule).
        """
        t0 = time.monotonic()
        out = KGOutput(query_kind="influencer_rank",
                       target={"topic_id": topic_id, "top_k": top_k,
                                "since_days": since_days})
        if not self.kuzu:
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out
        g, meta = _account_reply_graph(self.kuzu, topic_id, since_days)
        if g is None or g.number_of_edges() == 0:
            out.metrics = {"analyzed_node_count": 0, **meta}
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        try:
            import networkx as nx  # type: ignore
            scores = nx.pagerank(g, weight="weight", max_iter=100)
        except Exception as exc:
            log.error("kg_analytics.pagerank_error", error=str(exc)[:160])
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_accounts = {str(acc_id) for acc_id, _score in ranked[:top_k]}
        for acc_id, score in ranked[:top_k]:
            out.nodes.append(KGNode(
                id=acc_id, label="Account",
                properties={"pagerank": round(float(score), 6),
                             "in_degree": int(g.in_degree(acc_id)),
                             "out_degree": int(g.out_degree(acc_id)),
                             "role": "top"},
            ))
        _append_influencer_context_edges(
            out, g, {str(k): float(v) for k, v in scores.items()},
            top_accounts,
        )
        # `analyzed_*` describes the graph the algorithm ran over (the input);
        # `len(out.nodes)/len(out.edges)` is what was actually returned to the
        # caller. Keeping them as separate keys avoids the LLM/UI mismatch
        # where metrics["node_count"]=N got described as "N nodes returned".
        out.metrics = {
            "analyzed_node_count": g.number_of_nodes(),
            "analyzed_edge_count": g.number_of_edges(),
            **meta,
        }
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out

    # ── bridge_accounts ──────────────────────────────────────────────────────

    def bridge_accounts(
        self, top_k: int = 10, since_days: Optional[int] = 30,
    ) -> KGOutput:
        """Betweenness centrality over the global account-reply graph.

        High betweenness = sits on many shortest paths between other
        accounts; classic "bridge" between communities.
        """
        t0 = time.monotonic()
        out = KGOutput(query_kind="bridge_accounts",
                       target={"top_k": top_k, "since_days": since_days})
        if not self.kuzu:
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out
        g, meta = _account_reply_graph(self.kuzu, topic_id=None,
                                        since_days=since_days)
        if g is None or g.number_of_nodes() < 3:
            out.metrics = {"analyzed_node_count": getattr(
                               g, "number_of_nodes", lambda: 0)(),
                           "reason": "graph_too_small", **meta}
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        try:
            import networkx as nx  # type: ignore
            scores = nx.betweenness_centrality(g, normalized=True)
        except Exception as exc:
            log.error("kg_analytics.betweenness_error",
                      error=str(exc)[:160])
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        for acc_id, score in ranked[:top_k]:
            out.nodes.append(KGNode(
                id=acc_id, label="Account",
                properties={"betweenness": round(float(score), 6),
                             "in_degree": int(g.in_degree(acc_id)),
                             "out_degree": int(g.out_degree(acc_id))},
            ))
        out.metrics = {
            "analyzed_node_count": g.number_of_nodes(),
            "analyzed_edge_count": g.number_of_edges(),
            **meta,
        }
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out

    # ── coordinated_groups ───────────────────────────────────────────────────

    def coordinated_groups(
        self,
        topic_id: Optional[str] = None,
        min_size: int = 3,
        since_days: Optional[int] = 30,
    ) -> KGOutput:
        """Louvain communities on the (undirected) account-reply graph.

        A community is "coordinated" when ≥ min_size accounts cluster
        together by their reply patterns. Replaces the naive
        community_relations co-posting count from Phase 3.
        """
        t0 = time.monotonic()
        out = KGOutput(query_kind="coordinated_groups",
                       target={"topic_id": topic_id, "min_size": min_size,
                                "since_days": since_days})
        if not self.kuzu:
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out
        g, meta = _account_reply_graph(self.kuzu, topic_id, since_days)
        if g is None or g.number_of_edges() == 0:
            out.metrics = {"community_count": 0, **meta}
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        try:
            import community as community_louvain  # type: ignore
        except ImportError:
            log.warning("kg_analytics.louvain_missing")
            out.metrics = {"community_count": 0, "reason": "louvain_missing"}
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        try:
            partition = community_louvain.best_partition(g.to_undirected())
        except Exception as exc:
            log.error("kg_analytics.louvain_error", error=str(exc)[:160])
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        # Group nodes by community id
        comms: dict[int, list[str]] = {}
        for node, comm in partition.items():
            comms.setdefault(int(comm), []).append(str(node))

        # Keep only communities meeting size threshold; emit nodes with
        # community_id property and intra-community edges.
        kept_comms = {cid: members for cid, members in comms.items()
                       if len(members) >= min_size}
        for cid, members in kept_comms.items():
            for m in members:
                out.nodes.append(KGNode(
                    id=m, label="Account",
                    properties={"community_id": cid,
                                 "community_size": len(members)},
                ))
        # Edges that stay inside one community
        for u, v in g.edges():
            if u in partition and v in partition and partition[u] == partition[v]:
                if int(partition[u]) in kept_comms:
                    out.edges.append(KGEdge(
                        source_id=str(u), target_id=str(v),
                        rel_type="ReplyWithin",
                        properties={"community_id": int(partition[u])},
                    ))
        out.metrics = {
            "community_count": len(kept_comms),
            "total_communities": len(comms),
            "analyzed_node_count": g.number_of_nodes(),
            "analyzed_edge_count": g.number_of_edges(),
            **meta,
        }
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out

    # ── echo_chamber ─────────────────────────────────────────────────────────

    def echo_chamber(
        self,
        topic_id: str,
        modularity_threshold: float = 0.3,
    ) -> KGOutput:
        """Echo-chamber score for a topic.

        Score = community modularity of the within-topic post reply graph.
        High modularity (≥ threshold) means replies stay inside tight
        clusters - users mostly talk to people who agree with them rather
        than across community boundaries.
        """
        t0 = time.monotonic()
        out = KGOutput(query_kind="echo_chamber",
                       target={"topic_id": topic_id,
                                "modularity_threshold": modularity_threshold})
        if not self.kuzu:
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out
        g = _topic_post_reply_graph(self.kuzu, topic_id)
        if g is None or g.number_of_edges() < 2:
            out.metrics = {"is_echo_chamber": False,
                           "reason": "graph_too_small"}
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        try:
            import community as community_louvain  # type: ignore
            import networkx as nx  # type: ignore
        except ImportError:
            out.metrics = {"is_echo_chamber": False,
                           "reason": "deps_missing"}
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        try:
            ug = g.to_undirected()
            partition = community_louvain.best_partition(ug)
            modularity = community_louvain.modularity(partition, ug)
        except Exception as exc:
            log.error("kg_analytics.echo_modularity_error",
                      error=str(exc)[:160])
            out.metrics = {"is_echo_chamber": False, "reason": "calc_error"}
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        # Project the partition into KGNode/KGEdge so the UI can render the
        # cluster structure and the LLM/UI agree on counts. Cap output to the
        # top-N largest communities so a 300-node graph doesn't blow up the
        # report payload.
        comm_sizes: dict[int, int] = {}
        for _node, comm in partition.items():
            comm_sizes[int(comm)] = comm_sizes.get(int(comm), 0) + 1
        top_communities = sorted(
            comm_sizes.items(), key=lambda kv: kv[1], reverse=True,
        )[:_ECHO_TOP_COMMUNITIES]
        kept_comm_ids = {cid for cid, _ in top_communities}

        emitted_nodes = 0
        for node_id, comm in partition.items():
            if int(comm) not in kept_comm_ids:
                continue
            if emitted_nodes >= _ECHO_NODE_CAP:
                break
            out.nodes.append(KGNode(
                id=str(node_id), label="Post",
                properties={"community_id": int(comm),
                             "community_size": comm_sizes[int(comm)]},
            ))
            emitted_nodes += 1

        kept_node_ids = {n.id for n in out.nodes}
        for u, v in g.edges():
            su, sv = str(u), str(v)
            if su not in kept_node_ids or sv not in kept_node_ids:
                continue
            same_comm = partition.get(u) == partition.get(v)
            out.edges.append(KGEdge(
                source_id=su, target_id=sv,
                rel_type="ReplyWithin" if same_comm else "ReplyAcross",
                properties={"intra_community": bool(same_comm)},
            ))
            if len(out.edges) >= _ECHO_EDGE_CAP:
                break

        # `analyzed_*` describes the input graph the modularity was computed
        # over; out.nodes/out.edges describe what is actually returned (capped
        # by _ECHO_NODE_CAP / _ECHO_EDGE_CAP). Keep them as separate keys so
        # the LLM does not conflate "graph size" with "query result size".
        out.metrics = {
            "modularity": round(float(modularity), 4),
            "is_echo_chamber": modularity >= modularity_threshold,
            "community_count": len(set(partition.values())),
            "analyzed_node_count": g.number_of_nodes(),
            "analyzed_edge_count": g.number_of_edges(),
        }
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out
