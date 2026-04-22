"""ClaimStatusCapability — 'Is this claim true / false / unverifiable?'

Routes to a specific claim inside a run (by claim_id, or the
intervention_decision.primary_claim_id if omitted) and reports the
evidence balance with a constrained verdict label. Optionally augments
with Wikipedia / news if the internal Chroma layer yielded nothing.

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
    retrieve_official_sources, RetrieveOfficialSourcesInput,
    OfficialSource,
)


VerdictLabel = Literal[
    "supported", "contradicted", "disputed",
    "insufficient", "non_factual",
]


class ClaimStatusInput(CapabilityInput):
    run_id: str = "latest"
    claim_id: Optional[str] = None
    include_official_sources: bool = False


class ClaimStatusOutput(CapabilityOutput):
    run_id: str
    source: str
    claim_id: str
    claim_text: str
    verdict_label: VerdictLabel
    claim_actionability: Optional[str] = None
    non_actionable_reason: Optional[str] = None
    supporting_count: int = 0
    contradicting_count: int = 0
    uncertain_count: int = 0
    evidence_tiers: dict[str, int] = Field(default_factory=dict)
    top_supporting: list[EvidenceItem] = Field(default_factory=list)
    top_contradicting: list[EvidenceItem] = Field(default_factory=list)
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


class ClaimStatusCapability(Capability):
    name = "claim_status"
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
                # Fall back to the first claim in the run if no primary is set.
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

        official: list[OfficialSource] = []
        if input.include_official_sources and verdict in (
            "insufficient", "disputed",
        ):
            try:
                official = retrieve_official_sources(
                    RetrieveOfficialSourcesInput(
                        query_text=claim.normalized_text,
                        news_max_results=3,
                    )
                ).sources
            except Exception:  # noqa: BLE001
                official = []

        return ClaimStatusOutput(
            run_id=run_id,
            source=src,
            claim_id=claim.claim_id,
            claim_text=claim.normalized_text,
            verdict_label=verdict,
            claim_actionability=claim.claim_actionability,
            non_actionable_reason=claim.non_actionable_reason,
            supporting_count=len(supporting),
            contradicting_count=len(contradicting),
            uncertain_count=len(uncertain),
            evidence_tiers=_tier_histogram_from_details(
                supporting, contradicting, uncertain
            ),
            top_supporting=supporting[:3],
            top_contradicting=contradicting[:3],
            official_sources=official,
        )


register_capability(ClaimStatusCapability())
