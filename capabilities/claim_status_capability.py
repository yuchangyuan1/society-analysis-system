"""ClaimStatusCapability — 'Is this claim true / false / unverifiable?'

Hybrid strategy per `complete_project_transformation_plan.md` §5:

- **Batch half** (done by `agents/precompute_pipeline.py::KnowledgeAgent`):
  supporting / contradicting / uncertain evidence attached to the Claim.
- **On-demand half** (this capability, at query time):
    1. Re-query Chroma articles for fresh chunks scoped to the claim text.
    2. Optionally hit Wikipedia + NewsSearch for authoritative sources.
    3. Fuse counts + tier histogram into a 5-tier verdict ladder.
    4. Emit a deterministic `verdict_rationale` so the AnswerComposer can
       read a grounded explanation without re-reasoning about the data.

We explicitly AVOID absolute truth claims — the verdict is a ladder:
  supported    — contradicting == 0 and supporting >= 1
  contradicted — contradicting >= 1 and supporting == 0
  disputed     — both sides have evidence
  insufficient — no supporting / contradicting evidence
  non_factual  — claim_actionability says so (e.g. opinion, slogan)
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from capabilities.base import (
    Capability, CapabilityInput, CapabilityOutput,
    CapabilityError, register_capability,
)
from tools.run_query_tools import (
    get_claims, GetClaimsInput,
    get_claim_details, GetClaimDetailsInput,
    get_primary_claim, GetPrimaryClaimInput,
    EvidenceItem,
)
from tools.evidence_tools import (
    retrieve_evidence_chunks, RetrieveEvidenceChunksInput,
    retrieve_official_sources, RetrieveOfficialSourcesInput,
    EvidenceChunk, OfficialSource,
)


VerdictLabel = Literal[
    "supported", "contradicted", "disputed",
    "insufficient", "non_factual",
]


class ClaimStatusInput(CapabilityInput):
    run_id: str = "latest"
    claim_id: Optional[str] = None
    include_official_sources: bool = True
    include_community_posts: bool = True
    community_posts_k: int = 5


class ClaimStatusOutput(CapabilityOutput):
    run_id: str
    source: str
    claim_id: str
    claim_text: str
    verdict_label: VerdictLabel
    verdict_rationale: str = ""
    claim_actionability: Optional[str] = None
    non_actionable_reason: Optional[str] = None
    supporting_count: int = 0
    contradicting_count: int = 0
    uncertain_count: int = 0
    evidence_tiers: dict[str, int] = Field(default_factory=dict)
    top_supporting: list[EvidenceItem] = Field(default_factory=list)
    top_contradicting: list[EvidenceItem] = Field(default_factory=list)
    retrieved_chunks: list[EvidenceChunk] = Field(default_factory=list)
    official_sources: list[OfficialSource] = Field(default_factory=list)


def _decide_verdict(
    actionability: Optional[str],
    supporting: int,
    contradicting: int,
) -> VerdictLabel:
    if actionability == "non_actionable":
        return "non_factual"
    if supporting == 0 and contradicting == 0:
        return "insufficient"
    if supporting >= 1 and contradicting == 0:
        return "supported"
    if contradicting >= 1 and supporting == 0:
        return "contradicted"
    return "disputed"


def _tier_histogram_from_details(
    supporting: list[EvidenceItem],
    contradicting: list[EvidenceItem],
    uncertain: list[EvidenceItem],
) -> dict[str, int]:
    hist: dict[str, int] = {}
    for group in (supporting, contradicting, uncertain):
        for ev in group:
            tier = ev.source_tier or "internal_chroma"
            hist[tier] = hist.get(tier, 0) + 1
    return hist


def _compose_rationale(
    verdict: VerdictLabel,
    supporting: int,
    contradicting: int,
    uncertain: int,
    tier_hist: dict[str, int],
    retrieved_chunks: list[EvidenceChunk],
    official_sources: list[OfficialSource],
    actionability: Optional[str],
    non_actionable_reason: Optional[str],
) -> str:
    """Deterministic, grounded rationale string — no LLM, no hallucination.

    The Composer reads this verbatim when it wants a short 'why' clause.
    """
    if verdict == "non_factual":
        base = "Claim is classified as non_factual"
        if non_actionable_reason:
            base += f" ({non_actionable_reason})"
        return f"{base}; fact-check ladder does not apply."

    parts = [
        f"Precomputed evidence: {supporting} supporting, "
        f"{contradicting} contradicting, {uncertain} uncertain."
    ]

    if tier_hist:
        tier_str = ", ".join(
            f"{tier}={cnt}" for tier, cnt in sorted(tier_hist.items())
        )
        parts.append(f"Source tiers — {tier_str}.")

    if retrieved_chunks:
        parts.append(
            f"On-demand Chroma retrieval returned {len(retrieved_chunks)} "
            f"relevant chunk(s) for this claim."
        )
    else:
        parts.append("On-demand Chroma retrieval returned no new chunks.")

    if official_sources:
        wiki_n = sum(1 for s in official_sources if s.tier == "wikipedia")
        news_n = sum(1 for s in official_sources if s.tier == "news")
        parts.append(
            f"Authoritative lookup: {wiki_n} Wikipedia + {news_n} news article(s)."
        )

    verdict_blurb = {
        "supported": "Evidence leans supporting with no contradiction on record.",
        "contradicted": "Evidence leans contradicting with no support on record.",
        "disputed": "Both sides have citations — treat as disputed, not resolved.",
        "insufficient": "Too little evidence to rule either way.",
    }.get(verdict, "")
    if verdict_blurb:
        parts.append(verdict_blurb)

    return " ".join(parts)


class ClaimStatusCapability(Capability):
    name = "claim_status"
    description = (
        "Fact-check a specific claim: retrieve official evidence + community "
        "posts, produce a 5-tier verdict (supported / contradicted / disputed "
        "/ insufficient / non_factual), include citations. Answers 'is X true', "
        "'is this a rumor', 'what does the evidence say about X', "
        "'fact check this claim'."
    )
    example_utterances = [
        "这个说法是真的吗",
        "is X a rumor",
        "what's the evidence for claim X",
        "fact check",
        "有证据吗",
    ]
    tags = ["claim", "verdict", "evidence", "fact_check"]
    Input = ClaimStatusInput
    Output = ClaimStatusOutput

    def run(self, input: ClaimStatusInput) -> ClaimStatusOutput:
        # 1. Resolve which claim to look at.
        if input.claim_id:
            details = get_claim_details(
                GetClaimDetailsInput(
                    run_id=input.run_id, claim_id=input.claim_id,
                )
            )
            claim = details.claim
            run_id = details.run_id
            src = details.source
        else:
            primary = get_primary_claim(GetPrimaryClaimInput(run_id=input.run_id))
            if primary.claim is None:
                all_claims = get_claims(GetClaimsInput(run_id=input.run_id))
                if not all_claims.claims:
                    raise CapabilityError(
                        f"no claims found in run {input.run_id}"
                    )
                first_id = all_claims.claims[0].claim_id
                details = get_claim_details(
                    GetClaimDetailsInput(
                        run_id=input.run_id, claim_id=first_id,
                    )
                )
                claim = details.claim
                run_id = details.run_id
                src = details.source
            else:
                claim = primary.claim
                run_id = primary.run_id
                src = primary.source

        supporting = claim.supporting_evidence
        contradicting = claim.contradicting_evidence
        uncertain = claim.uncertain_evidence

        verdict = _decide_verdict(
            claim.claim_actionability,
            len(supporting),
            len(contradicting),
        )

        # 2. On-demand community-post retrieval (Chroma articles collection).
        retrieved: list[EvidenceChunk] = []
        if input.include_community_posts and verdict != "non_factual":
            try:
                retrieved = retrieve_evidence_chunks(
                    RetrieveEvidenceChunksInput(
                        query_text=claim.normalized_text,
                        n_results=input.community_posts_k,
                    )
                ).chunks
            except Exception:  # noqa: BLE001
                retrieved = []

        # 3. On-demand authoritative sources (Wikipedia + NewsSearch).
        official: list[OfficialSource] = []
        if input.include_official_sources and verdict != "non_factual":
            try:
                official = retrieve_official_sources(
                    RetrieveOfficialSourcesInput(
                        query_text=claim.normalized_text,
                        news_max_results=3,
                    )
                ).sources
            except Exception:  # noqa: BLE001
                official = []

        tier_hist = _tier_histogram_from_details(
            supporting, contradicting, uncertain
        )
        rationale = _compose_rationale(
            verdict=verdict,
            supporting=len(supporting),
            contradicting=len(contradicting),
            uncertain=len(uncertain),
            tier_hist=tier_hist,
            retrieved_chunks=retrieved,
            official_sources=official,
            actionability=claim.claim_actionability,
            non_actionable_reason=claim.non_actionable_reason,
        )

        return ClaimStatusOutput(
            run_id=run_id,
            source=src,
            claim_id=claim.claim_id,
            claim_text=claim.normalized_text,
            verdict_label=verdict,
            verdict_rationale=rationale,
            claim_actionability=claim.claim_actionability,
            non_actionable_reason=claim.non_actionable_reason,
            supporting_count=len(supporting),
            contradicting_count=len(contradicting),
            uncertain_count=len(uncertain),
            evidence_tiers=tier_hist,
            top_supporting=supporting[:3],
            top_contradicting=contradicting[:3],
            retrieved_chunks=retrieved,
            official_sources=official,
        )


register_capability(ClaimStatusCapability())
