"""
Rule-based claim actionability classifier (P0-1, final_project_transformation_plan.md §10.3).

Two-class output:
  - actionable     — we hold at least one contradicting evidence item and can
                     draft a concrete rebuttal.
  - non_actionable — we cannot responsibly generate a rebuttal card. Paired with
                     one of three reason codes so downstream output can explain
                     *why* instead of silently skipping.

Priority order (first match wins):
  1. contradicting_evidence_count >= 1          → actionable
  2. matches opinion/hedging pattern + no anchors → non_factual_expression
  3. proper_noun_count == 0                      → context_sparse
  4. no wikipedia or internal_chroma tier hits   → context_sparse
  5. fallback                                    → insufficient_evidence

No LLM is involved. This is deterministic and cheap to run on every claim
after evidence retrieval.
"""
from __future__ import annotations

import re

from models.claim import Claim, ClaimActionability, NonActionableReason
from services.wikipedia_service import WikipediaService


# Verbs / phrasings that hint at editorial interpretation rather than a falsifiable
# factual statement. Matched case-insensitively on the claim's normalized text.
_OPINION_PATTERN = re.compile(
    r"\b(?:reveals?|reveal|exposes?|expose|suggests?|suggest|"
    r"appears?|appear|seems?|seem|proves?|proof\s+of|"
    r"shows?\s+how|demonstrates?\s+how|hints?\s+at)\b",
    re.IGNORECASE,
)

# Digits or an explicit date-ish token. Presence means "there is something
# verifiable here" and we should not flag as pure non_factual_expression.
_NUMERIC_ANCHOR_RE = re.compile(r"\b\d")


def _has_factual_anchor(text: str) -> bool:
    """
    An opinion-pattern sentence still counts as factual if it carries a number,
    date, or proper noun — those provide something verifiable.
    """
    if _NUMERIC_ANCHOR_RE.search(text):
        return True
    if WikipediaService.count_proper_nouns(text) > 0:
        return True
    return False


def classify_claim_actionability(
    claim: Claim,
) -> tuple[ClaimActionability, NonActionableReason | None]:
    """
    Apply the §10.3 rules to a single claim. Writes nothing; caller is
    responsible for assigning to claim.claim_actionability /
    claim.non_actionable_reason.
    """
    text = claim.normalized_text or ""

    if len(claim.contradicting_evidence) >= 1:
        return "actionable", None

    if _OPINION_PATTERN.search(text) and not _has_factual_anchor(text):
        return "non_actionable", "non_factual_expression"

    if WikipediaService.count_proper_nouns(text) == 0:
        return "non_actionable", "context_sparse"

    wiki_or_chroma_hits = sum(
        1
        for bucket in (
            claim.supporting_evidence,
            claim.contradicting_evidence,
            claim.uncertain_evidence,
        )
        for ev in bucket
        if ev.source_tier in ("wikipedia", "internal_chroma")
    )
    if wiki_or_chroma_hits == 0:
        return "non_actionable", "context_sparse"

    return "non_actionable", "insufficient_evidence"


def annotate_claims(claims: list[Claim]) -> dict[str, int]:
    """
    Classify a batch of claims in-place and return a distribution summary:
      {
        "actionable": int,
        "non_actionable": int,
        "context_sparse": int,
        "insufficient_evidence": int,
        "non_factual_expression": int,
      }
    """
    summary = {
        "actionable": 0,
        "non_actionable": 0,
        "context_sparse": 0,
        "insufficient_evidence": 0,
        "non_factual_expression": 0,
    }
    for claim in claims:
        actionability, reason = classify_claim_actionability(claim)
        claim.claim_actionability = actionability
        claim.non_actionable_reason = reason
        summary[actionability] += 1
        if reason:
            summary[reason] += 1
    return summary
