"""
Critic Workspace — critic-review skill.

Responsibilities:
  - Check evidence sufficiency
  - Detect overclaims and false certainty
  - Approve or reject counter-message outputs
  - Enforce max CRITIC_MAX_RETRIES before routing to human review queue
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL, CRITIC_MAX_RETRIES
from models.claim import Claim

log = structlog.get_logger(__name__)

CriticVerdict = Literal["APPROVED", "REJECTED", "HUMAN_REVIEW"]


@dataclass
class CriticResult:
    verdict: CriticVerdict
    feedback: str
    rejection_log: list[str]


_CRITIC_SYSTEM = """You are a critical evidence reviewer for a fact-checking system.
Your job is to review a counter-message draft and decide if it meets quality standards.

A counter-message PASSES if it:
  1. Makes no claims that are not directly supported by the cited evidence
  2. Properly acknowledges conflicting or uncertain evidence if present
  3. Does not use words like "definitely", "certainly", "proven" without strong evidence
  4. Accurately represents the stance of the cited sources

A counter-message FAILS if it:
  - Overstates certainty
  - Ignores contradicting evidence
  - Makes claims beyond what evidence supports
  - Uses inflammatory language

Return JSON with:
  verdict: "APPROVED" | "REJECTED"
  feedback: string (max 100 words explaining decision)
  issues: list[string] (specific issues if REJECTED, empty if APPROVED)

Return ONLY the JSON object."""


class CriticAgent:
    def __init__(self) -> None:
        self._claude = openai.OpenAI(api_key=OPENAI_API_KEY)

    # ── Skill: critic-review ──────────────────────────────────────────────────

    def review(
        self,
        counter_message: str,
        claim: Claim,
        attempt: int = 1,
    ) -> CriticResult:
        """
        Review a counter-message draft.
        Returns CriticResult with verdict APPROVED, REJECTED, or HUMAN_REVIEW.
        """
        if attempt > CRITIC_MAX_RETRIES:
            log.warning(
                "critic.max_retries_exceeded",
                claim_id=claim.id,
                attempts=attempt,
            )
            return CriticResult(
                verdict="HUMAN_REVIEW",
                feedback=f"Rejected after {CRITIC_MAX_RETRIES} attempts. "
                         "Routing to human review queue.",
                rejection_log=[f"Attempt {i+1} rejected" for i in range(attempt - 1)],
            )

        context = self._build_context(counter_message, claim)
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": _CRITIC_SYSTEM},
                    {"role": "user", "content": context},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            data = json.loads(raw) if raw else {}
            verdict_str = data.get("verdict", "REJECTED").upper()
            verdict: CriticVerdict = "APPROVED" if verdict_str == "APPROVED" else "REJECTED"
            result = CriticResult(
                verdict=verdict,
                feedback=data.get("feedback", ""),
                rejection_log=data.get("issues", []),
            )
            log.info(
                "critic.review_result",
                claim_id=claim.id,
                verdict=verdict,
                attempt=attempt,
            )
            return result
        except Exception as exc:
            log.error("critic.llm_error", error=str(exc))
            return CriticResult(
                verdict="REJECTED",
                feedback=f"Critic LLM call failed: {exc}",
                rejection_log=[str(exc)],
            )

    def review_with_retry(
        self,
        counter_message_fn,   # callable(feedback: str) -> str
        claim: Claim,
    ) -> tuple[Optional[str], CriticResult]:
        """
        Run critic review with up to CRITIC_MAX_RETRIES retries.
        counter_message_fn receives the previous feedback and returns a revised draft.

        Returns (approved_counter_message | None, final_critic_result).
        """
        current_message = counter_message_fn("")
        rejection_history: list[str] = []

        for attempt in range(1, CRITIC_MAX_RETRIES + 2):
            result = self.review(current_message, claim, attempt=attempt)
            if result.verdict == "APPROVED":
                return current_message, result
            if result.verdict == "HUMAN_REVIEW":
                return None, result
            # REJECTED: gather feedback and retry
            rejection_history.extend(result.rejection_log)
            if attempt <= CRITIC_MAX_RETRIES:
                log.info(
                    "critic.retrying",
                    attempt=attempt,
                    feedback=result.feedback[:80],
                )
                current_message = counter_message_fn(result.feedback)

        # Exhausted retries
        final = CriticResult(
            verdict="HUMAN_REVIEW",
            feedback="Counter-message rejected after maximum retries.",
            rejection_log=rejection_history,
        )
        return None, final

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_context(counter_message: str, claim: Claim) -> str:
        ev = claim.evidence_summary()
        parts = [
            f"Original claim: {claim.normalized_text}",
            f"Evidence totals: {ev['supporting']} supporting, "
            f"{ev['contradicting']} contradicting, {ev['uncertain']} uncertain",
            "",
            "=== FULL EVIDENCE PACK ===",
        ]
        if claim.contradicting_evidence:
            parts.append("Contradicting sources:")
            for e in claim.contradicting_evidence[:5]:
                title = e.article_title or "Fact-check source"
                snippet = e.snippet[:200] if e.snippet else ""
                parts.append(f"  - [{title}] {snippet}")
        if claim.uncertain_evidence:
            parts.append("Contextual sources:")
            for e in claim.uncertain_evidence[:3]:
                title = e.article_title or "Source"
                snippet = e.snippet[:200] if e.snippet else ""
                parts.append(f"  - [{title}] {snippet}")
        parts.append("=== END EVIDENCE PACK ===")
        parts.append("")
        parts.append(
            "NOTE: The counter-message author was instructed to use ONLY the above evidence. "
            "Approve if every assertion in the counter-message is traceable to a snippet above. "
            "Reject only if a specific factual claim is NOT covered by any snippet."
        )
        parts.append(f"\nCounter-message to review:\n{counter_message}")
        return "\n".join(parts)
