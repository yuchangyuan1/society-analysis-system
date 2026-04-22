"""/runs — list runs and fetch per-run summary (manifest + metrics).

No agents/* or services/* imports — read-only over data/runs/{run_id}/.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/runs", tags=["runs"])


def _runs_root(request: Request) -> Path:
    return request.app.state.runs_root


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _run_summary(run_dir: Path) -> dict[str, Any]:
    manifest = _load_json(run_dir / "run_manifest.json") or {}
    metrics = _load_json(run_dir / "metrics.json") or {}
    return {
        "run_id": manifest.get("run_id") or run_dir.name,
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "query_text": manifest.get("query_text"),
        "subreddits": manifest.get("subreddits", []),
        "openai_model": manifest.get("openai_model"),
        "git_sha": manifest.get("git_sha"),
        "post_count": manifest.get("post_count"),
        "report_id": manifest.get("report_id"),
        "metrics": {
            "evidence_coverage": metrics.get("evidence_coverage"),
            "community_modularity_q": metrics.get("community_modularity_q"),
            "counter_effect_closed_loop_rate": metrics.get(
                "counter_effect_closed_loop_rate"
            ),
        },
        "has_report": (run_dir / "report.md").exists(),
        "has_raw": (run_dir / "report_raw.json").exists(),
        "has_metrics": (run_dir / "metrics.json").exists(),
    }


@router.get("")
def list_runs(request: Request) -> dict[str, Any]:
    root = _runs_root(request)
    if not root.exists():
        return {"runs_root": str(root), "count": 0, "runs": []}
    entries: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        entries.append(_run_summary(child))
    return {"runs_root": str(root), "count": len(entries), "runs": entries}


@router.get("/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    run_dir = _runs_root(request) / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    manifest = _load_json(run_dir / "run_manifest.json") or {}
    metrics = _load_json(run_dir / "metrics.json") or {}
    visuals_dir = run_dir / "counter_visuals"
    visuals = (
        sorted([p.name for p in visuals_dir.iterdir() if p.is_file()])
        if visuals_dir.exists()
        else []
    )
    return {
        "run_id": run_id,
        "manifest": manifest,
        "metrics": metrics,
        "artifacts": {
            "report_md": (run_dir / "report.md").exists(),
            "report_raw_json": (run_dir / "report_raw.json").exists(),
            "metrics_json": (run_dir / "metrics.json").exists(),
            "run_manifest_json": (run_dir / "run_manifest.json").exists(),
            "counter_visuals": visuals,
        },
    }
