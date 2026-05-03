"""
RunManifest - tracks per-run identity, git sha, source parameters, and the
post-snapshot fingerprint. Restored as a slim v2 model in Phase 5 cleanup
(the v1 file was deleted along with the rest of the v1 surface).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RunManifest(BaseModel):
    schema_version: str = "v2"
    run_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    git_sha: Optional[str] = None
    openai_model: Optional[str] = None
    query_text: Optional[str] = None
    subreddits: list[str] = Field(default_factory=list)
    reddit_query: Optional[str] = None
    reddit_sort: Optional[str] = None
    jsonl_path: Optional[str] = None
    image_url: Optional[str] = None
    image_path: Optional[str] = None
    days_back: int = 7
    thresholds: dict = Field(default_factory=dict)
    posts_snapshot_sha256: Optional[str] = None
    post_count: int = 0
    report_id: Optional[str] = None
    # production hardening Day 4: commit state for crash-safe rollback.
    #   pending   = ManifestService.new_run() set this; pipeline still running
    #   committed = ManifestService.finalize() set this when all stages OK
    #   failed    = ManifestService.mark_failed() set this on exception
    commit_state: str = "pending"
