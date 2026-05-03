"""
ManifestService - produces and persists a RunManifest for each pipeline run.

Layout on disk (v2):
    data/runs/{run_id}/
        run_manifest.json      (this service)
        run_manifest_v2.json   (PrecomputePipelineV2)
"""
from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

import config
from models.manifest import RunManifest

log = structlog.get_logger(__name__)


class ManifestService:
    def __init__(self, runs_root: Optional[Path] = None) -> None:
        self._runs_root = runs_root or Path(config.RUNS_DIR)

    def runs_root(self) -> Path:
        self._runs_root.mkdir(parents=True, exist_ok=True)
        return self._runs_root

    def run_dir(self, run_id: str) -> Path:
        p = self.runs_root() / run_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def new_run(
        self,
        query_text: Optional[str],
        subreddits: Optional[list[str]] = None,
        reddit_query: Optional[str] = None,
        reddit_sort: Optional[str] = None,
        jsonl_path: Optional[str] = None,
        image_url: Optional[str] = None,
        image_path: Optional[str] = None,
        days_back: int = 7,
    ) -> RunManifest:
        started = datetime.utcnow()
        run_id = self._build_run_id(
            started, query_text, subreddits, reddit_query, jsonl_path
        )
        thresholds = {
            "nl2sql_conflict_sim_low": getattr(
                config, "NL2SQL_CONFLICT_SIM_LOW", 0.92,
            ),
            "nl2sql_conflict_sim_high": getattr(
                config, "NL2SQL_CONFLICT_SIM_HIGH", 0.95,
            ),
        }
        manifest = RunManifest(
            run_id=run_id,
            started_at=started,
            git_sha=_git_sha(),
            openai_model=config.OPENAI_MODEL,
            query_text=query_text,
            subreddits=list(subreddits or []),
            reddit_query=reddit_query,
            reddit_sort=reddit_sort,
            jsonl_path=jsonl_path,
            image_url=image_url,
            image_path=image_path,
            days_back=days_back,
            thresholds=thresholds,
            commit_state="pending",
        )
        # Create directory + pending sentinel so the rollback scanner sees us.
        self.run_dir(run_id)
        self._write(manifest)
        log.info("manifest.new_run", run_id=run_id, query=(query_text or "")[:60])
        return manifest

    def finalize(
        self,
        manifest: RunManifest,
        posts_snapshot_sha256: Optional[str] = None,
        post_count: int = 0,
        report_id: Optional[str] = None,
    ) -> Path:
        manifest.finished_at = datetime.utcnow()
        manifest.posts_snapshot_sha256 = posts_snapshot_sha256
        manifest.post_count = post_count
        manifest.report_id = report_id
        manifest.commit_state = "committed"
        path = self._write(manifest)
        log.info("manifest.finalized", run_id=manifest.run_id, path=str(path))
        return path

    def mark_failed(self, manifest: RunManifest, error: str = "") -> Path:
        """Set commit_state=failed; lets the rollback scanner pick it up."""
        manifest.finished_at = datetime.utcnow()
        manifest.commit_state = "failed"
        path = self._write(manifest)
        log.warning("manifest.failed", run_id=manifest.run_id,
                    error=error[:200])
        return path

    def list_pending_runs(self) -> list[RunManifest]:
        """Scan runs/ for any run still in commit_state='pending' or 'failed'.
        Used by scripts/data_admin scan-pending and pipeline startup sweeps."""
        out: list[RunManifest] = []
        if not self.runs_root().exists():
            return out
        for d in self.runs_root().iterdir():
            if not d.is_dir():
                continue
            mf = d / "run_manifest.json"
            if not mf.exists():
                continue
            try:
                m = RunManifest.model_validate_json(mf.read_text(encoding="utf-8"))
                if m.commit_state in ("pending", "failed"):
                    out.append(m)
            except Exception as exc:
                log.warning("manifest.list_pending_parse_error",
                            path=str(mf), error=str(exc)[:120])
        return out

    def _write(self, manifest: RunManifest) -> Path:
        path = self.run_dir(manifest.run_id) / "run_manifest.json"
        path.write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8",
        )
        return path

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_run_id(
        started: datetime,
        query_text: Optional[str],
        subreddits: Optional[list[str]],
        reddit_query: Optional[str],
        jsonl_path: Optional[str],
    ) -> str:
        ts = started.strftime("%Y%m%d-%H%M%S")
        key = "|".join([
            query_text or "",
            ",".join(sorted(subreddits or [])),
            reddit_query or "",
            jsonl_path or "",
        ])
        short = hashlib.sha256(key.encode("utf-8")).hexdigest()[:6]
        return f"{ts}-{short}"


def hash_posts_snapshot(posts: list) -> str:
    """Compute sha256 over (account_id, text) of each post — stable across runs."""
    hasher = hashlib.sha256()
    for p in posts:
        account = getattr(p, "account_id", "") or ""
        text = getattr(p, "text", "") or ""
        hasher.update(f"{account}\x1f{text}\x1e".encode("utf-8"))
    return hasher.hexdigest()


def _git_sha() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).parent.parent),
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = (out.stdout or "").strip()
        return sha or None
    except Exception:
        return None
