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
        channel: Optional[str] = None,
        jsonl_path: Optional[str] = None,
        image_url: Optional[str] = None,
        image_path: Optional[str] = None,
        days_back: int = 7,
    ) -> RunManifest:
        started = datetime.utcnow()
        run_id = self._build_run_id(
            started, query_text, subreddits, reddit_query, channel, jsonl_path
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
            channel=channel,
            jsonl_path=jsonl_path,
            image_url=image_url,
            image_path=image_path,
            days_back=days_back,
            thresholds=thresholds,
        )
        # Create directory so downstream writers can rely on it
        self.run_dir(run_id)
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

        path = self.run_dir(manifest.run_id) / "run_manifest.json"
        path.write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        log.info("manifest.finalized", run_id=manifest.run_id, path=str(path))
        return path

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_run_id(
        started: datetime,
        query_text: Optional[str],
        subreddits: Optional[list[str]],
        reddit_query: Optional[str],
        channel: Optional[str],
        jsonl_path: Optional[str],
    ) -> str:
        ts = started.strftime("%Y%m%d-%H%M%S")
        key = "|".join([
            query_text or "",
            ",".join(sorted(subreddits or [])),
            reddit_query or "",
            channel or "",
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
