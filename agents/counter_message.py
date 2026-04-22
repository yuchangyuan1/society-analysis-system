"""
Counter-Message Workspace — counter-message-build skill.

Responsibilities:
  - Generate evidence-backed rebuttal text for a misinformation claim
  - Produce clear, factual, shareable counter-narrative
"""
from __future__ import annotations

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.claim import Claim
from models.risk_assessment import RiskAssessment

log = structlog.get_logger(__name__)

_CM_SYSTEM = """You are a fact-checking communications expert writing evidence-grounded rebuttals.

CRITICAL RULE — Evidence boundary: Base your counter-message EXCLUSIVELY on the evidence
snippets provided in the EVIDENCE PACK section of the user message. Do NOT draw on your
training knowledge as a factual source. If a point is not covered by the provided snippets,
omit it — do not invent or infer supporting facts.

The counter-message must:
  1. Be factual and neutral in tone (not preachy or aggressive)
  2. Only assert claims that appear directly in the provided evidence snippets
  3. Reference the source name (e.g. "WHO states..." or "Per FCC:...") for each assertion
  4. Be under 280 characters if possible; hard cap at 500 characters for detail
  5. End with: "Verify before sharing."
  6. If evidence is limited, write a narrowly scoped message — better a short accurate
     message than a longer one with unsupported claims

Return ONLY the counter-message text. No JSON. No preamble."""


class CounterMessageAgent:
    def __init__(self) -> None:
        self._claude = openai.OpenAI(api_key=OPENAI_API_KEY)

    # ── Skill: counter-message-build ──────────────────────────────────────────

    def build_counter_message(
        self,
        claim: Claim,
        risk: RiskAssessment,
        max_chars: int = 500,
        revision_feedback: str = "",
    ) -> str:
        """
        Generate a rebuttal text for the given claim.
        Returns the counter-message string.

        revision_feedback — optional critic feedback from a prior attempt;
            included as revision instructions without mutating the Claim object.
        Raises ValueError if evidence is insufficient (should be blocked upstream).
        """
        if not claim.has_sufficient_evidence():
            raise ValueError(
                f"Claim {claim.id} has insufficient evidence to counter-message."
            )

        context = self._build_context(claim, risk, revision_feedback)
        try:
            response = self._claude.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=256,
                messages=[
                    {"role": "system", "content": _CM_SYSTEM},
                    {"role": "user", "content": context},
                ],
            )
            text = (response.choices[0].message.content or "").strip()
            if len(text) > max_chars:
                text = text[:max_chars].rsplit(" ", 1)[0] + "…"
            log.info(
                "counter_message.built",
                claim_id=claim.id,
                length=len(text),
            )
            return text
        except Exception as exc:
            log.error("counter_message.error", error=str(exc))
            raise

    # ── Internal ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_context(
        claim: Claim,
        risk: RiskAssessment,
        revision_feedback: str = "",
    ) -> str:
        parts = [
            "Misinformation claim spreading on social media:",
            f'  "{claim.normalized_text}"',
            f"Risk level: {risk.risk_level.value}",
            "",
            "=== EVIDENCE PACK (use ONLY these sources for factual claims) ===",
        ]

        ev_count = 0
        if claim.contradicting_evidence:
            parts.append("Sources that CONTRADICT this claim:")
            for ev in claim.contradicting_evidence[:4]:
                title = ev.article_title or "Fact-check source"
                snippet = ev.snippet[:200] if ev.snippet else "(no snippet)"
                url = f" [{ev.article_url}]" if ev.article_url else ""
                parts.append(f"  [{ev_count + 1}] {title}{url}\n      Excerpt: {snippet}")
                ev_count += 1
        if claim.uncertain_evidence:
            parts.append("Contextually relevant sources (stance uncertain):")
            for ev in claim.uncertain_evidence[:2]:
                title = ev.article_title or "Source"
                snippet = ev.snippet[:200] if ev.snippet else "(no snippet)"
                parts.append(f"  [{ev_count + 1}] {title}\n      Excerpt: {snippet}")
                ev_count += 1
        if ev_count == 0:
            parts.append("  (no evidence retrieved — write only what is directly and generally known to be false)")

        parts.append("=== END EVIDENCE PACK ===")
        parts.append("")
        parts.append(
            "Write a counter-message using ONLY the evidence items above. "
            "Do not introduce facts not present in the excerpts."
        )

        if revision_feedback:
            parts.append(
                f"\n[REVISION REQUEST] The previous draft was rejected by the critic. "
                f"Address this specific feedback and stay within the evidence pack: {revision_feedback}"
            )
        return "\n".join(parts)
