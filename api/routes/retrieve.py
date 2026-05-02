"""
/retrieve/{evidence,nl2sql,kg} - redesign-2026-05 Phase 3.7.

Lets each Phase 3 branch be invoked independently, both for debug /
evaluation and for external integrations that don't want to go through
Query Rewrite + Planner.

The handlers stay thin: they construct the branch tool, delegate, and
return the Pydantic output untouched. Lazy construction keeps cold-start
cheap when only one branch is exercised.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/retrieve", tags=["retrieve"])


# ── A. Evidence ──────────────────────────────────────────────────────────────

class EvidenceRequest(BaseModel):
    query: str
    metadata_filter: Optional[dict] = None
    rerank: bool = True


@router.post("/evidence")
def retrieve_evidence(req: EvidenceRequest) -> dict[str, Any]:
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="empty query")
    try:
        from tools.hybrid_retrieval import HybridRetriever
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"branch unavailable: {exc}")
    bundle = HybridRetriever().retrieve(
        req.query,
        metadata_filter=req.metadata_filter,
        rerank=req.rerank,
    )
    return {"branch": "evidence", "bundle": bundle.model_dump()}


# ── B. NL2SQL ────────────────────────────────────────────────────────────────

class NL2SQLRequest(BaseModel):
    nl_query: str = Field(..., description="natural-language question")


@router.post("/nl2sql")
def retrieve_nl2sql(req: NL2SQLRequest) -> dict[str, Any]:
    if not req.nl_query.strip():
        raise HTTPException(status_code=400, detail="empty query")
    try:
        from tools.nl2sql_tools import NL2SQLTool
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"branch unavailable: {exc}")
    out = NL2SQLTool().answer(req.nl_query)
    return {"branch": "nl2sql", "sql_output": out.model_dump()}


# ── C. KG Query ──────────────────────────────────────────────────────────────

class KGRequest(BaseModel):
    query_kind: str  # propagation_path | key_nodes | community_relations | topic_correlation
    target: dict[str, Any] = Field(default_factory=dict)


@router.post("/kg")
def retrieve_kg(req: KGRequest) -> dict[str, Any]:
    try:
        from tools.kg_query_tools import KGQueryTool
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"branch unavailable: {exc}")
    tool = KGQueryTool()
    if req.query_kind == "propagation_path":
        out = tool.propagation_path(
            source_account=req.target.get("source_account", ""),
            target_account=req.target.get("target_account", ""),
            max_hops=int(req.target.get("max_hops", 4)),
        )
    elif req.query_kind == "key_nodes":
        out = tool.key_nodes(
            topic_id=req.target.get("topic_id", ""),
            top_k=int(req.target.get("top_k", 10)),
        )
    elif req.query_kind == "community_relations":
        out = tool.community_relations(
            topic_id=req.target.get("topic_id", ""),
            min_shared_posts=int(req.target.get("min_shared_posts", 2)),
        )
    elif req.query_kind == "topic_correlation":
        out = tool.topic_correlation(
            topic_a=req.target.get("topic_a", ""),
            topic_b=req.target.get("topic_b", ""),
        )
    else:
        raise HTTPException(status_code=400,
                            detail=f"unknown query_kind: {req.query_kind}")
    return {"branch": "kg", "kg_output": out.model_dump()}
