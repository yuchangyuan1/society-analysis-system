"""Thin HTTP client around the local FastAPI research API."""
from __future__ import annotations

import os
from typing import Any

import requests

API_BASE = os.getenv("RESEARCH_API_BASE", "http://127.0.0.1:8000")
_TIMEOUT = float(os.getenv("RESEARCH_API_TIMEOUT", "10"))
# /chat/query runs full LLM orchestration (rewriter -> planner -> branches ->
# report writer -> critic) and can take 1-3 minutes. Read-only GETs use the
# short _TIMEOUT; long-running POSTs default to this floor unless overridden.
_LONG_POST_TIMEOUT = float(os.getenv("RESEARCH_API_LONG_TIMEOUT", "240"))


def _get_json(path: str) -> dict[str, Any]:
    resp = requests.get(f"{API_BASE}{path}", timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _get_text(path: str) -> str:
    resp = requests.get(f"{API_BASE}{path}", timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _post_json(path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    resp = requests.post(
        f"{API_BASE}{path}",
        json=payload,
        timeout=timeout if timeout is not None else _LONG_POST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def health() -> dict[str, Any]:
    return _get_json("/health")


def list_runs() -> list[dict[str, Any]]:
    return _get_json("/runs").get("runs", [])


def get_run(run_id: str) -> dict[str, Any]:
    return _get_json(f"/runs/{run_id}")


def get_report_md(run_id: str) -> str:
    return _get_text(f"/runs/{run_id}/report")


def get_report_raw(run_id: str) -> dict[str, Any]:
    return _get_json(f"/runs/{run_id}/raw")


def get_metrics(run_id: str) -> dict[str, Any]:
    return _get_json(f"/runs/{run_id}/metrics")


def visual_url(run_id: str, filename: str) -> str:
    return f"{API_BASE}/runs/{run_id}/visual/{filename}"


def chat_query(session_id: str, message: str) -> dict[str, Any]:
    """Send a chat turn. Returns ChatResponse-shaped dict."""
    return _post_json("/chat/query", {"session_id": session_id, "message": message})


def get_session(session_id: str) -> dict[str, Any]:
    return _get_json(f"/chat/session/{session_id}")


def import_reddit(payload: dict[str, Any]) -> dict[str, Any]:
    return _post_json("/admin/import/reddit", payload, timeout=15.0)


def import_official(payload: dict[str, Any]) -> dict[str, Any]:
    return _post_json("/admin/import/official", payload, timeout=15.0)


def get_import_job(job_id: str) -> dict[str, Any]:
    return _get_json(f"/admin/import/jobs/{job_id}")
