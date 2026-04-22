"""Tools for reading run artefacts — list runs, fetch summaries, topics, claims.

These tools wrap `data/runs/{run_id}/` and `sample_runs/{run_id}/` so that
capabilities don't need to know about file paths or JSON schemas.

Paths are resolved relative to config.RUNS_DIR for the "data" source and
config.BASE_DIR/"sample_runs" for the "sample" source. A run_id may exist
in either tree; lookup falls back to "data" first, then "sample".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

import config
from tools.base import ToolInput, ToolOutput, ToolInputError


_DATA_ROOT = Path(config.RUNS_DIR)
_SAMPLE_ROOT = Path(config.BASE_DIR) / "sample_runs"

Source = Literal["data", "sample"]


# ─── Models ──────────────────────────────────────────────────────────────────

class RunBrief(BaseModel):
    run_id: str
    source: Source
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    query_text: Optional[str] = None
    post_count: Optional[int] = None
    report_id: Optional[str] = None
    evidence_coverage: Optional[float] = None
    community_modularity_q: Optional[float] = None
    has_report: bool = False
    has_raw: bool = False
    has_metrics: bool = False


class ListRunsInput(ToolInput):
    include_samples: bool = True


class ListRunsOutput(ToolOutput):
    runs: list[RunBrief] = Field(default_factory=list)


class GetRunSummaryInput(ToolInput):
    run_id: str = "latest"


class GetRunSummaryOutput(ToolOutput):
    run_id: str
    source: Source
    manifest: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    counter_visuals: list[str] = Field(default_factory=list)


class TopicBrief(BaseModel):
    topic_id: str
    label: str
    post_count: int = 0
    velocity: float = 0.0
    is_trending: bool = False
    misinfo_risk: float = 0.0
    dominant_emotion: str = ""
    emotion_distribution: dict[str, float] = Field(default_factory=dict)
    representative_claims: list[str] = Field(default_factory=list)


class GetTopicsInput(ToolInput):
    run_id: str = "latest"
    top_k: Optional[int] = None
    sort_by: Literal["post_count", "velocity", "misinfo_risk"] = "post_count"


class GetTopicsOutput(ToolOutput):
    run_id: str
    source: Source
    topics: list[TopicBrief] = Field(default_factory=list)


class ClaimBrief(BaseModel):
    claim_id: str
    normalized_text: str
    claim_actionability: Optional[str] = None
    non_actionable_reason: Optional[str] = None
    supporting_count: int = 0
    contradicting_count: int = 0
    uncertain_count: int = 0
    evidence_tiers: dict[str, int] = Field(default_factory=dict)


class GetClaimsInput(ToolInput):
    run_id: str = "latest"


class GetClaimsOutput(ToolOutput):
    run_id: str
    source: Source
    claims: list[ClaimBrief] = Field(default_factory=list)
    primary_claim_id: Optional[str] = None


class GetClaimDetailsInput(ToolInput):
    run_id: str = "latest"
    claim_id: str


class EvidenceItem(BaseModel):
    article_id: str
    article_title: Optional[str] = None
    article_url: Optional[str] = None
    source_name: Optional[str] = None
    stance: str
    source_tier: str = "internal_chroma"
    snippet: Optional[str] = None


class ClaimDetails(BaseModel):
    claim_id: str
    normalized_text: str
    claim_actionability: Optional[str] = None
    non_actionable_reason: Optional[str] = None
    supporting_evidence: list[EvidenceItem] = Field(default_factory=list)
    contradicting_evidence: list[EvidenceItem] = Field(default_factory=list)
    uncertain_evidence: list[EvidenceItem] = Field(default_factory=list)


class GetClaimDetailsOutput(ToolOutput):
    run_id: str
    source: Source
    claim: ClaimDetails


class GetPrimaryClaimInput(ToolInput):
    run_id: str = "latest"


class GetPrimaryClaimOutput(ToolOutput):
    run_id: str
    source: Source
    primary_claim_id: Optional[str] = None
    claim: Optional[ClaimDetails] = None


# ─── Path helpers ────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_run_dir(run_id: str) -> tuple[Path, Source]:
    """Locate run_id in data/runs first, then sample_runs.

    Supports run_id='latest' to mean the newest dir under data/runs.
    Raises ToolInputError if nothing matches.
    """
    if run_id == "latest":
        if _DATA_ROOT.exists():
            candidates = sorted(
                (c for c in _DATA_ROOT.iterdir() if c.is_dir()),
                key=lambda c: c.name,
                reverse=True,
            )
            if candidates:
                return candidates[0], "data"
        raise ToolInputError("no runs found under data/runs/")

    data_dir = _DATA_ROOT / run_id
    if data_dir.is_dir():
        return data_dir, "data"

    sample_dir = _SAMPLE_ROOT / run_id
    if sample_dir.is_dir():
        return sample_dir, "sample"

    raise ToolInputError(f"run_id not found in data/ or sample_runs/: {run_id}")


def _run_brief_from_dir(run_dir: Path, source: Source) -> RunBrief:
    manifest = _load_json(run_dir / "run_manifest.json") or {}
    metrics = _load_json(run_dir / "metrics.json") or {}
    return RunBrief(
        run_id=manifest.get("run_id") or run_dir.name,
        source=source,
        started_at=manifest.get("started_at"),
        finished_at=manifest.get("finished_at"),
        query_text=manifest.get("query_text"),
        post_count=manifest.get("post_count"),
        report_id=manifest.get("report_id"),
        evidence_coverage=metrics.get("evidence_coverage"),
        community_modularity_q=metrics.get("community_modularity_q"),
        has_report=(run_dir / "report.md").exists(),
        has_raw=(run_dir / "report_raw.json").exists(),
        has_metrics=(run_dir / "metrics.json").exists(),
    )


# ─── Tool functions ──────────────────────────────────────────────────────────

def list_runs(input: ListRunsInput) -> ListRunsOutput:
    """Enumerate runs across data/runs/ and sample_runs/."""
    entries: list[RunBrief] = []
    for root, source in [(_DATA_ROOT, "data"), (_SAMPLE_ROOT, "sample")]:
        if source == "sample" and not input.include_samples:
            continue
        if not root.exists():
            continue
        for child in sorted(root.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            entries.append(_run_brief_from_dir(child, source))
    return ListRunsOutput(runs=entries)


def get_run_summary(input: GetRunSummaryInput) -> GetRunSummaryOutput:
    """Return manifest + metrics + counter_visuals filenames for one run."""
    run_dir, source = _resolve_run_dir(input.run_id)
    manifest = _load_json(run_dir / "run_manifest.json") or {}
    metrics = _load_json(run_dir / "metrics.json") or {}
    visuals_dir = run_dir / "counter_visuals"
    visuals = (
        sorted([p.name for p in visuals_dir.iterdir() if p.is_file()])
        if visuals_dir.exists()
        else []
    )
    return GetRunSummaryOutput(
        run_id=manifest.get("run_id") or run_dir.name,
        source=source,
        manifest=manifest,
        metrics=metrics,
        counter_visuals=visuals,
    )


def get_topics(input: GetTopicsInput) -> GetTopicsOutput:
    """Return topics from report_raw.json::topic_summaries, sorted + truncated."""
    run_dir, source = _resolve_run_dir(input.run_id)
    raw = _load_json(run_dir / "report_raw.json")
    if raw is None:
        raise ToolInputError(
            f"report_raw.json missing for run_id={input.run_id}"
        )

    raw_topics = raw.get("topic_summaries") or []
    topics = [
        TopicBrief(
            topic_id=t.get("topic_id", ""),
            label=t.get("label", ""),
            post_count=t.get("post_count", 0),
            velocity=t.get("velocity", 0.0),
            is_trending=t.get("is_trending", False),
            misinfo_risk=t.get("misinfo_risk", 0.0),
            dominant_emotion=t.get("dominant_emotion", ""),
            emotion_distribution=t.get("emotion_distribution") or {},
            representative_claims=t.get("representative_claims") or [],
        )
        for t in raw_topics
    ]

    topics.sort(
        key=lambda tb: getattr(tb, input.sort_by, 0),
        reverse=True,
    )
    if input.top_k is not None:
        topics = topics[: input.top_k]

    return GetTopicsOutput(
        run_id=run_dir.name,
        source=source,
        topics=topics,
    )


def _tier_histogram(evidence_list: list[dict[str, Any]]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for ev in evidence_list:
        tier = ev.get("source_tier") or "internal_chroma"
        hist[tier] = hist.get(tier, 0) + 1
    return hist


def _evidence_item(ev: dict[str, Any], stance: str) -> EvidenceItem:
    return EvidenceItem(
        article_id=ev.get("article_id", ""),
        article_title=ev.get("article_title"),
        article_url=ev.get("article_url"),
        source_name=ev.get("source_name"),
        stance=stance,
        source_tier=ev.get("source_tier") or "internal_chroma",
        snippet=ev.get("snippet"),
    )


def _primary_claim_id(raw: dict[str, Any]) -> Optional[str]:
    decision = raw.get("intervention_decision") or {}
    return decision.get("primary_claim_id")


def get_claims(input: GetClaimsInput) -> GetClaimsOutput:
    """Return structured claims from report_raw.json::claims."""
    run_dir, source = _resolve_run_dir(input.run_id)
    raw = _load_json(run_dir / "report_raw.json")
    if raw is None:
        raise ToolInputError(
            f"report_raw.json missing for run_id={input.run_id}"
        )

    claims_raw = raw.get("claims") or []
    claims: list[ClaimBrief] = []
    for c in claims_raw:
        supporting = c.get("supporting_evidence") or []
        contradicting = c.get("contradicting_evidence") or []
        uncertain = c.get("uncertain_evidence") or []
        tiers: dict[str, int] = {}
        for group in (supporting, contradicting, uncertain):
            for k, v in _tier_histogram(group).items():
                tiers[k] = tiers.get(k, 0) + v
        claims.append(
            ClaimBrief(
                claim_id=c.get("id", ""),
                normalized_text=c.get("normalized_text", ""),
                claim_actionability=c.get("claim_actionability"),
                non_actionable_reason=c.get("non_actionable_reason"),
                supporting_count=len(supporting),
                contradicting_count=len(contradicting),
                uncertain_count=len(uncertain),
                evidence_tiers=tiers,
            )
        )

    return GetClaimsOutput(
        run_id=run_dir.name,
        source=source,
        claims=claims,
        primary_claim_id=_primary_claim_id(raw),
    )


def _claim_details_from_raw(c: dict[str, Any]) -> ClaimDetails:
    return ClaimDetails(
        claim_id=c.get("id", ""),
        normalized_text=c.get("normalized_text", ""),
        claim_actionability=c.get("claim_actionability"),
        non_actionable_reason=c.get("non_actionable_reason"),
        supporting_evidence=[
            _evidence_item(ev, "supports")
            for ev in (c.get("supporting_evidence") or [])
        ],
        contradicting_evidence=[
            _evidence_item(ev, "contradicts")
            for ev in (c.get("contradicting_evidence") or [])
        ],
        uncertain_evidence=[
            _evidence_item(ev, "neutral")
            for ev in (c.get("uncertain_evidence") or [])
        ],
    )


def get_claim_details(input: GetClaimDetailsInput) -> GetClaimDetailsOutput:
    """Return a single claim with all evidence, found by claim_id."""
    run_dir, source = _resolve_run_dir(input.run_id)
    raw = _load_json(run_dir / "report_raw.json")
    if raw is None:
        raise ToolInputError(
            f"report_raw.json missing for run_id={input.run_id}"
        )

    for c in raw.get("claims") or []:
        if c.get("id") == input.claim_id:
            return GetClaimDetailsOutput(
                run_id=run_dir.name,
                source=source,
                claim=_claim_details_from_raw(c),
            )

    raise ToolInputError(
        f"claim_id not found in run {input.run_id}: {input.claim_id}"
    )


def get_primary_claim(input: GetPrimaryClaimInput) -> GetPrimaryClaimOutput:
    """Return the intervention_decision.primary_claim_id + its full details."""
    run_dir, source = _resolve_run_dir(input.run_id)
    raw = _load_json(run_dir / "report_raw.json")
    if raw is None:
        raise ToolInputError(
            f"report_raw.json missing for run_id={input.run_id}"
        )

    primary_id = _primary_claim_id(raw)
    if not primary_id:
        return GetPrimaryClaimOutput(
            run_id=run_dir.name,
            source=source,
            primary_claim_id=None,
            claim=None,
        )

    for c in raw.get("claims") or []:
        if c.get("id") == primary_id:
            return GetPrimaryClaimOutput(
                run_id=run_dir.name,
                source=source,
                primary_claim_id=primary_id,
                claim=_claim_details_from_raw(c),
            )

    return GetPrimaryClaimOutput(
        run_id=run_dir.name,
        source=source,
        primary_claim_id=primary_id,
        claim=None,
    )
