"""
/health/* - extended diagnostics endpoints.

`/health/kg` reports node / edge counts per Kuzu type, the most recent
Replied edge timestamp (proxied via Postgres posts_v2.ingested_at since
Kuzu doesn't store edge timestamps), and the in-memory subgraph cache
hit rate. Used by the Reflection panel KG tab and external monitors.
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


_NODE_QUERIES = [
    ("Account",  "MATCH (a:Account) RETURN count(a)"),
    ("Post",     "MATCH (p:Post) RETURN count(p)"),
    ("Topic",    "MATCH (t:Topic) RETURN count(t)"),
    ("Entity",   "MATCH (e:Entity) RETURN count(e)"),
]
_EDGE_QUERIES = [
    ("Posted",         "MATCH ()-[r:Posted]->() RETURN count(r)"),
    ("Replied",        "MATCH ()-[r:Replied]->() RETURN count(r)"),
    ("BelongsToTopic", "MATCH ()-[r:BelongsToTopic]->() RETURN count(r)"),
    ("HasEntity",      "MATCH ()-[r:HasEntity]->() RETURN count(r)"),
]


@router.get("/metrics")
def metrics_snapshot() -> dict[str, Any]:
    """Counters + histogram quantiles for the Reflection Performance tab."""
    from services.metrics import metrics
    return metrics.snapshot()


@router.get("/kg")
def kg_diagnostics() -> dict[str, Any]:
    """Node / edge counts + cache stats for the Knowledge Graph branch."""
    nodes: dict[str, int] = {}
    edges: dict[str, int] = {}

    try:
        from services.kuzu_service import KuzuService
        kuzu = KuzuService()
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"kuzu unavailable: {exc}")

    for name, q in _NODE_QUERIES:
        try:
            row = kuzu._safe_execute(q)  # type: ignore[attr-defined]
            nodes[name] = int(((row or [{}])[0] or {}).get(q.split()[-1], 0)
                              or list(((row or [{}])[0] or {}).values())[0]
                              or 0)
        except Exception as exc:
            log.warning("health_kg.node_count_error", node=name,
                        error=str(exc)[:120])
            nodes[name] = -1

    for name, q in _EDGE_QUERIES:
        try:
            row = kuzu._safe_execute(q)  # type: ignore[attr-defined]
            edges[name] = int(((row or [{}])[0] or {}).get(q.split()[-1], 0)
                              or list(((row or [{}])[0] or {}).values())[0]
                              or 0)
        except Exception as exc:
            log.warning("health_kg.edge_count_error", edge=name,
                        error=str(exc)[:120])
            edges[name] = -1

    # Replied freshness: proxy via posts_v2.ingested_at MAX where parent
    # references existed. Best-effort.
    last_reply_ingest: str = ""
    try:
        from services.postgres_service import PostgresService
        pg = PostgresService()
        pg.connect()
        with pg.cursor() as cur:
            # We don't have a separate reply table in v2; the freshness
            # proxy is the most recent posts_v2 row that was a reply.
            # In the v2 model we store parent_post_id only on the Kuzu
            # side, so we approximate with the most recent ingestion.
            cur.execute(
                "SELECT MAX(ingested_at) AS last_at FROM posts_v2"
            )
            row = cur.fetchone() or {}
            last_at = row.get("last_at")
            if last_at:
                last_reply_ingest = last_at.isoformat()
    except Exception as exc:
        log.warning("health_kg.pg_freshness_error", error=str(exc)[:120])

    # Cache stats
    cache_stats: dict[str, Any] = {}
    try:
        from services.kg_cache import SUBGRAPH_CACHE
        cache_stats = SUBGRAPH_CACHE.stats()
    except Exception as exc:
        log.warning("health_kg.cache_error", error=str(exc)[:120])

    # Health verdict
    healthy = (
        all(v >= 0 for v in nodes.values())
        and all(v >= 0 for v in edges.values())
    )
    warnings: list[str] = []
    if edges.get("Replied", 0) == 0:
        warnings.append(
            "No Replied edges. KG propagation queries will return empty. "
            "Run the v2 pipeline against data with comment chains."
        )
    if nodes.get("Account", 0) == 0:
        warnings.append("No Account nodes - has the pipeline ever run?")
    if nodes.get("Topic", 0) == 0:
        warnings.append("No Topic nodes - clustering may not have produced output.")

    return {
        "ok": healthy,
        "nodes": nodes,
        "edges": edges,
        "last_post_ingested_at": last_reply_ingest,
        "cache": cache_stats,
        "warnings": warnings,
    }
