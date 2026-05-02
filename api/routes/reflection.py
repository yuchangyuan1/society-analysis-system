"""
/reflection/* - redesign-2026-05 Phase 5.4.

Read-only API for the Reflection Streamlit panel. Exposes:
    GET  /reflection/chroma2          - list NL2SQL records
    GET  /reflection/chroma3          - list Planner memory records
    GET  /reflection/log              - list reflection_log audit rows
    DELETE /reflection/chroma2/{id}   - manual purge
    DELETE /reflection/chroma3/{id}   - manual purge

Authentication: none (debug endpoint). Production should put this behind
the same auth layer as `/admin/*`.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/reflection", tags=["reflection"])


def _get_collection(name: str):
    from services.chroma_collections import ChromaCollections
    cols = ChromaCollections()
    if name == "chroma2":
        return cols.nl2sql
    if name == "chroma3":
        return cols.planner
    raise HTTPException(status_code=400, detail=f"unknown collection {name}")


def _list(handle, kind: Optional[str], limit: int) -> list[dict]:
    where = {"kind": kind} if kind else None
    raw = handle.handle.get(
        where=where, include=["documents", "metadatas"],
    )
    out: list[dict] = []
    ids = list(raw.get("ids") or [])
    docs = list(raw.get("documents") or [])
    metas = list(raw.get("metadatas") or [])
    for i, rid in enumerate(ids[:limit]):
        out.append({
            "id": rid,
            "document": docs[i] if i < len(docs) else "",
            "metadata": metas[i] if i < len(metas) else {},
        })
    return out


@router.get("/chroma2")
def list_chroma2(
    kind: Optional[str] = Query(None,
                                description="schema | success | error"),
    limit: int = Query(200, le=1000),
) -> dict[str, Any]:
    handle = _get_collection("chroma2")
    return {"records": _list(handle, kind, limit)}


@router.get("/chroma3")
def list_chroma3(
    kind: Optional[str] = Query(
        None,
        description="module_card | workflow_success | workflow_error | composition_error",
    ),
    limit: int = Query(200, le=1000),
) -> dict[str, Any]:
    handle = _get_collection("chroma3")
    return {"records": _list(handle, kind, limit)}


@router.delete("/chroma2/{record_id}")
def delete_chroma2(record_id: str) -> dict[str, Any]:
    handle = _get_collection("chroma2")
    handle.delete(ids=[record_id])
    return {"deleted": record_id}


@router.delete("/chroma3/{record_id}")
def delete_chroma3(record_id: str) -> dict[str, Any]:
    handle = _get_collection("chroma3")
    handle.delete(ids=[record_id])
    return {"deleted": record_id}


@router.get("/log")
def list_log(
    error_kind: Optional[str] = None,
    limit: int = Query(100, le=500),
) -> dict[str, Any]:
    try:
        from services.postgres_service import PostgresService
        pg = PostgresService()
        pg.connect()
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail=f"reflection_log unavailable: {exc}")
    rows: list[dict] = []
    try:
        with pg.cursor() as cur:
            if error_kind:
                cur.execute(
                    "SELECT * FROM reflection_log WHERE error_kind = %s "
                    "ORDER BY occurred_at DESC LIMIT %s",
                    (error_kind, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM reflection_log "
                    "ORDER BY occurred_at DESC LIMIT %s",
                    (limit,),
                )
            rows = list(cur.fetchall())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"rows": rows}
