"""
Query rewrite contracts - redesign-2026-05 Phase 4.1.

Output of `agents/query_rewriter.py`. The Planner consumes a list of
`Subtask`s; each carries an intent hint plus an initial branch set so the
Router can collapse to "decide who" while the Planner decides "how to call
them" (PROJECT_REDESIGN_V2.md 5c Router vs Planner).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


BranchName = Literal["evidence", "nl2sql", "kg"]


SubtaskIntent = Literal[
    "fact_check",          # verify a claim against official sources
    "topic_claim_audit",   # classify claims inside a topic vs official evidence
    "official_recap",      # what does an authoritative source say
    "community_count",     # filter / count / group on community posts
    "community_listing",   # show me the posts about X
    "trend",               # how is volume / sentiment changing
    "propagation",         # generic "who is amplifying" - kept for compat
    "comparison",          # contrast A vs B (multi-source)
    "explain_decision",    # why was X done (introspection)
    "freeform",            # fallback when no specific intent matches
    # ── redesign-2026-05-kg Phase C: KG-specialised intents ────────────────
    "propagation_trace",   # trace a reply chain between accounts / posts
    "influencer_query",    # who is most influential (PageRank, not post count)
    "coordination_check",  # are accounts coordinating? (Louvain communities)
    "community_structure", # echo chamber / cluster question
    "cascade_query",       # viral / longest thread / deepest cascade
]


class SubtaskTarget(BaseModel):
    run_id: Optional[str] = None
    topic_id: Optional[str] = None
    claim_id: Optional[str] = None
    account_id: Optional[str] = None
    timeframe: Optional[str] = None  # ISO range or natural-language hint
    metadata_filter: dict = Field(default_factory=dict)


class Subtask(BaseModel):
    """One atomic question that fits a single Planner workflow step."""

    text: str                              # rewritten, self-contained text
    intent: SubtaskIntent = "freeform"
    suggested_branches: list[BranchName] = Field(default_factory=list)
    targets: SubtaskTarget = Field(default_factory=SubtaskTarget)
    rationale: str = ""                    # why we picked this intent / branches


class RewrittenQuery(BaseModel):
    """Full output of Query Rewriter."""

    original: str
    subtasks: list[Subtask] = Field(default_factory=list)
    inherited_context: dict = Field(default_factory=dict)  # session current_*
    fallback_reason: Optional[str] = None  # set when LLM failed and we degraded

    @property
    def is_multistep(self) -> bool:
        return len(self.subtasks) > 1
