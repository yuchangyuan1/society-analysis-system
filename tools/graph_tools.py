"""Graph tools — Kuzu-first, with per-run JSON overlay fallback.

Per `complete_project_transformation_plan.md` §6:

- **Kuzu = canonical graph storage.** Online graph queries (community list,
  bridge accounts, coordinated pairs, topic ↔ account edges, post ↔ topic
  edges) are issued against Kuzu via Cypher.
- **`report_raw.json` = per-run analytical overlay** (dominant_emotion,
  is_echo_chamber, modularity, pre-computed isolation_score). Those fields
  are NOT currently stored in the Kuzu schema, so we read them from the
  run's `report_raw.json` and merge onto the Kuzu structural view.
- **NetworkX = debug / local visualization only.** This module does not
  build NetworkX graphs anymore.

Fallback behaviour: if Kuzu has no rows for a query (e.g. sample runs that
never populated Kuzu, or a cold Kuzu db), we degrade to reading
`report_raw.json::community_analysis` so the chat still works on demo
fixtures.
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
    # Backend selection for observability. "auto" = Kuzu-first with JSON
    # fallback; "kuzu" = Kuzu only (return empty on miss); "json" = legacy
    # report_raw.json path.
    backend: str = "auto"


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
    # Which backend actually served this query.
    backend_used: str = "json"


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


class GetTopicAccountsInput(ToolInput):
    topic_id: str
    limit: int = 50


class GetTopicAccountsOutput(ToolOutput):
    topic_id: str
    accounts: list[dict[str, Any]] = Field(default_factory=list)


class GetCoordinatedPairsInput(ToolInput):
    min_shared_claims: int = 2
    limit: int = 20


class GetCoordinatedPairsOutput(ToolOutput):
    pairs: list[dict[str, Any]] = Field(default_factory=list)


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


def _kuzu_or_none():
    """Lazy-import KuzuService to avoid pulling it into Tool import time."""
    try:
        from services.kuzu_service import KuzuService
        return KuzuService()
    except Exception:
        return None


def _community_views_from_kuzu(
    kuzu, topic_id: Optional[str], top_k: int
) -> list[CommunityView]:
    """Build CommunityView list from Kuzu rows. Returns [] if Kuzu empty."""
    rows = kuzu.get_communities() or []
    if not rows:
        return []

    # Augment with accounts per community (used to surface bridge_accounts).
    views: list[CommunityView] = []
    for row in rows:
        cid = row.get("community_id") or ""
        accounts = kuzu.get_community_accounts(cid) or []
        bridges = [
            a.get("username") or a.get("account_id") or ""
            for a in accounts
            if (a.get("role") or "").lower() == "bridge"
        ]
        views.append(CommunityView(
            community_id=cid,
            label=row.get("label") or "",
            size=int(row.get("size") or 0),
            isolation_score=float(row.get("isolation_score") or 0.0),
            bridge_accounts=bridges[:10],
        ))

    # Topic filter: keep communities whose accounts touch this topic.
    if topic_id:
        topic_accounts = {
            a.get("account_id")
            for a in (kuzu.get_accounts_for_topic(topic_id) or [])
        }
        views = [
            v for v in views
            if any(
                (a.get("account_id") in topic_accounts)
                for a in (kuzu.get_community_accounts(v.community_id) or [])
            )
        ]
        for v in views:
            v.dominant_topics = [topic_id]

    views.sort(key=lambda v: v.size, reverse=True)
    return views[:top_k]


def _overlay_analytics(
    views: list[CommunityView], raw: dict[str, Any]
) -> list[CommunityView]:
    """Merge non-structural analytics (emotion, echo_chamber) from report_raw.json."""
    community = raw.get("community_analysis") or {}
    overlays = {c.get("community_id"): c for c in (community.get("communities") or [])}
    for v in views:
        overlay = overlays.get(v.community_id)
        if not overlay:
            continue
        v.dominant_emotion = overlay.get("dominant_emotion", v.dominant_emotion)
        v.is_echo_chamber = bool(overlay.get("is_echo_chamber", v.is_echo_chamber))
        if not v.bridge_accounts:
            v.bridge_accounts = overlay.get("bridge_accounts") or []
        if not v.dominant_topics:
            v.dominant_topics = overlay.get("dominant_topics") or []
    return views


def _community_views_from_json(
    raw: dict[str, Any], topic_id: Optional[str], top_k: int,
) -> list[CommunityView]:
    community = raw.get("community_analysis") or {}
    communities_raw = community.get("communities") or []

    if topic_id:
        filtered = [
            c for c in communities_raw
            if topic_id in (c.get("dominant_topics") or [])
        ]
    else:
        filtered = list(communities_raw)

    filtered.sort(key=lambda c: c.get("size", 0), reverse=True)
    filtered = filtered[:top_k]

    return [
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


# ─── Tool functions ──────────────────────────────────────────────────────────

def query_topic_graph(input: QueryTopicGraphInput) -> QueryTopicGraphOutput:
    """Return community structure + coordination signals for a run/topic.

    Kuzu-first (§6). Falls back to report_raw.json when Kuzu is empty
    (sample-run fixtures, cold db).
    """
    run_dir, source = _resolve_run_dir(input.run_id)
    raw = _load_raw(run_dir)

    community_raw = raw.get("community_analysis") or {}
    propagation = raw.get("propagation_summary") or {}

    # Route through Kuzu unless explicitly disabled.
    views: list[CommunityView] = []
    backend_used = "json"

    if input.backend in ("auto", "kuzu"):
        kuzu = _kuzu_or_none()
        if kuzu is not None:
            views = _community_views_from_kuzu(
                kuzu, input.topic_id, input.top_k_communities
            )
            if views:
                views = _overlay_analytics(views, raw)
                backend_used = "kuzu"
                # Prefer Kuzu-derived coordination count when available.
                kuzu_pairs = kuzu.get_coordinated_accounts(min_shared_claims=2) or []
                kuzu_accounts = kuzu.get_all_accounts() or []
            else:
                kuzu_pairs = None
                kuzu_accounts = None
        else:
            kuzu_pairs = None
            kuzu_accounts = None
    else:
        kuzu_pairs = None
        kuzu_accounts = None

    # Fallback to JSON path for views (and for runs without Kuzu data).
    if not views and input.backend != "kuzu":
        views = _community_views_from_json(
            raw, input.topic_id, input.top_k_communities
        )
        backend_used = "json"

    coordinated_pairs = (
        len(kuzu_pairs) if kuzu_pairs is not None
        else propagation.get("coordinated_pairs", 0)
    )
    unique_accounts = (
        len(kuzu_accounts) if kuzu_accounts is not None
        else propagation.get("unique_accounts", 0)
    )

    return QueryTopicGraphOutput(
        run_id=run_dir.name,
        source=source,
        topic_id=input.topic_id,
        community_count=community_raw.get("community_count", len(views)),
        echo_chamber_count=community_raw.get(
            "echo_chamber_count",
            sum(1 for v in views if v.is_echo_chamber),
        ),
        modularity=community_raw.get("modularity"),
        communities=views,
        coordinated_pairs=coordinated_pairs,
        unique_accounts=unique_accounts,
        backend_used=backend_used,
    )


def get_propagation_summary(
    input: GetPropagationSummaryInput,
) -> GetPropagationSummaryOutput:
    """Return propagation_summary block from report_raw.json (raw dict).

    We keep this as JSON-read because the narrative fields (anomaly_description,
    account_role_summary) are precomputed per-run artefacts — they are the
    offline pipeline's output, not a Kuzu-derived query.
    """
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


def get_topic_accounts(input: GetTopicAccountsInput) -> GetTopicAccountsOutput:
    """Return accounts that posted in a topic, via Kuzu Post→Topic edges.

    New in the §6 rewrite: this one was previously unavailable as a Tool
    and had to be synthesized by the old JSON path.
    """
    kuzu = _kuzu_or_none()
    accounts: list[dict[str, Any]] = []
    if kuzu is not None:
        rows = kuzu.get_accounts_for_topic(input.topic_id) or []
        accounts = rows[: input.limit]
    return GetTopicAccountsOutput(topic_id=input.topic_id, accounts=accounts)


def get_coordinated_pairs(
    input: GetCoordinatedPairsInput,
) -> GetCoordinatedPairsOutput:
    """Return account pairs sharing at least N claims. Kuzu-first (§6)."""
    kuzu = _kuzu_or_none()
    pairs: list[dict[str, Any]] = []
    if kuzu is not None:
        pairs = kuzu.get_coordinated_accounts(
            min_shared_claims=input.min_shared_claims
        ) or []
    return GetCoordinatedPairsOutput(pairs=pairs[: input.limit])
