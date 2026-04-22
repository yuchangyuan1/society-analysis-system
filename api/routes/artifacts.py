"""/runs/{run_id}/{report,raw,metrics,visual} — raw artifact accessors."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

router = APIRouter(prefix="/runs/{run_id}", tags=["artifacts"])


def _run_dir(request: Request, run_id: str) -> Path:
    run_dir = request.app.state.runs_root / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return run_dir


def _require_file(run_dir: Path, name: str) -> Path:
    path = run_dir / name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{name} not found")
    return path


@router.get("/report", response_class=PlainTextResponse)
def get_report_md(run_id: str, request: Request) -> str:
    path = _require_file(_run_dir(request, run_id), "report.md")
    return path.read_text(encoding="utf-8")


@router.get("/raw")
def get_report_raw(run_id: str, request: Request) -> FileResponse:
    path = _require_file(_run_dir(request, run_id), "report_raw.json")
    return FileResponse(path, media_type="application/json")


@router.get("/metrics")
def get_metrics(run_id: str, request: Request) -> FileResponse:
    path = _require_file(_run_dir(request, run_id), "metrics.json")
    return FileResponse(path, media_type="application/json")


@router.get("/visual/{filename}")
def get_visual(run_id: str, filename: str, request: Request) -> FileResponse:
    run_dir = _run_dir(request, run_id)
    # Prevent traversal — only a bare filename is accepted
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = run_dir / "counter_visuals" / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="visual not found")
    return FileResponse(path)
