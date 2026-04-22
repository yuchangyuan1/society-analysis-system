"""
RunManifest — reproducibility record for a single pipeline invocation.

Every `python main.py ...` run creates one of these, written to
`data/runs/{run_id}/run_manifest.json` (see ManifestService).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RunManifest(BaseModel):
    run_id: str
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None

    # Reproducibility
    git_sha: Optional[str] = None
    openai_model: Optional[str] = None

    # Input parameters
    query_text: Optional[str] = None
    subreddits: list[str] = Field(default_factory=list)
    reddit_query: Optional[str] = None
    reddit_sort: Optional[str] = None
    channel: Optional[str] = None
    jsonl_path: Optional[str] = None
    image_url: Optional[str] = None
    image_path: Optional[str] = None
    days_back: int = 7

    # Key thresholds at time of run
    thresholds: dict[str, float] = Field(default_factory=dict)

    # Filled in at finalize()
    posts_snapshot_sha256: Optional[str] = None
    post_count: int = 0
    report_id: Optional[str] = None
