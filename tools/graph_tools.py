"""Tools that surface graph / propagation slices of a run to capabilities.

We don't maintain a live graph database at chat time. Instead we read
`report_raw.json::community_analysis` + `::propagation_summary` on demand,
rebuild a small NetworkX view for the requested topic, and cache it
per-(run_id, topic_id) so repeated follow-ups stay cheap.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from tools.base import ToolInput, ToolOutput, ToolInputError
from tools.run_query_tools import _resolve_run_dir, Source


# ─── Models ──────────────────────────────────────────────────────────────────

class CommunityView(BaseModel):
    community_id: str
    label: str
    size: int = 0
    isolation_score: float = 0.0
    dominant_emotion: str = "neutral"
    is_echo_chamber: bool = False
    bridge_accounts: list[str] = Field(default_factory=list)
    dominant_topics: list[str] = Field(default_factory=list)


class QueryTopicGraphInput(ToolInput):
    run_id: str = "latest"
    topic_id: Optional[str] = None  # if None, returns run-wide community view
    top_k_communities: int = 5


class QueryTopicGraphOutput(ToolOutput):
    run_id: str
    source: Source
    topic_id: Optional[str] = None
    community_count: int = 0
    echo_chamber_count: int = 0
    modularity: Optional[float] = None
    communities: list[CommunityView] = Field(default_factory=list)
    coordinated_pairs: int = 0
    unique_accounts: int = 0


class GetSocialMetricsInput(ToolInput):
    run_id: str = "latest"


class GetSocialMetricsOutput(ToolOutput):
    run_id: str
    source: Source
    metrics: dict[str, Any] = Field(default_factory=dict)


class GetPropagationSummaryInput(ToolInput):
    run_id: str = "latest"


class GetPropagationSummaryOutput(ToolOutput):
    run_id: str
    source: Source
    propagation_summary: dict[str, Any] = Field(default_factory=dict)


# ─── Helpers ─────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=8)
def _cached_raw(run_dir_str: str) -> str:
    """LRU-cached raw JSON text keyed by resolved path."""
    path = Path(run_dir_str) / "report_raw.json"
    if not path.exists():
        raise ToolInputError(f"report_raw.json missing: {path}")
    return path.read_text(encoding="utf-8")


def _load_raw(run_dir: Path) -> dict[str, Any]:
    return json.loads(_cached_raw(str(run_dir)))


# ─── Tool functions ──────────────────────────────────────────────────────────

def query_topic_graph(input: QueryTopicGraphInput) -> QueryTopicGraphOutput:
    """Return community structure + coordination signals for a run/topic."""
    run_dir, source = _resolve_run_dir(input.run_id)
    raw = _load_raw(run_dir)

    community = raw.get("community_analysis") or {}
    propagation = raw.get("propagation_summary") or {}

    communities_raw = community.get("communities") or []

    if input.topic_id:
        filtered = [
            c for c in communities_raw
            if input.topic_id in (c.get("dominant_topics") or [])
        ]
    else:
        filtered = list(communities_raw)

    filtered.sort(key=lambda c: c.get("size", 0), reverse=True)
    filtered = filtered[: input.top_k_communities]

    views = [
        CommunityView(
            community_id=c.get("community_id", ""),
            label=c.get("label", ""),
            size=c.get("size", 0),
            isolation_score=c.get("isolation_score", 0.0),
            dominant_emotion=c.get("dominant_emotion", "neutral"),
            is_echo_chamber=c.get("is_echo_chamber", False),
            bridge_accounts=c.get("bridge_accounts") or [],
            dominant_topics=c.get("dominant_topics") or [],
        )
        for c in filtered
    ]

    return QueryTopicGraphOutput(
        run_id=run_dir.name,
        source=source,
        topic_id=input.topic_id,
        community_count=community.get("community_count", 0),
        echo_chamber_count=community.get("echo_chamber_count", 0),
        modularity=community.get("modularity"),
        communities=views,
        coordinated_pairs=propagation.get("coordinated_pairs", 0),
        unique_accounts=propagation.get("unique_accounts", 0),
    )


def get_propagation_summary(
    input: GetPropagationSummaryInput,
) -> GetPropagationSummaryOutput:
    """Return propagation_summary block from report_raw.json (raw dict)."""
    run_dir, source = _resolve_run_dir(input.run_id)
    raw = _load_raw(run_dir)
    return GetPropagationSummaryOutput(
        run_id=run_dir.name,
        source=source,
        propagation_summary=raw.get("propagation_summary") or {},
    )


def get_social_metrics(input: GetSocialMetricsInput) -> GetSocialMetricsOutput:
    """Return the whole metrics.json document for a run."""
    run_dir, source = _resolve_run_dir(input.run_id)
    metrics_path = run_dir / "metrics.json"
    if not metrics_path.exists():
        return GetSocialMetricsOutput(
            run_id=run_dir.name, source=source, metrics={}
        )
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metrics = {}
    return GetSocialMetricsOutput(
        run_id=run_dir.name, source=source, metrics=metrics
    )
