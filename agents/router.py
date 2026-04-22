"""Intent router.

Classifies the user message into a capability intent and extracts
optional targets (run_id, topic_id, claim_id). Phase 1 recognises three
intents: `topic_overview`, `emotion_analysis`, `other`. Later phases
extend the literal set.

Uses OpenAI chat.completions with `response_format=json_object` for
deterministic parsing.
"""

from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field

import openai
from config import OPENAI_API_KEY, OPENAI_MODEL


RouterIntent = Literal[
    "topic_overview",
    "emotion_analysis",
    "claim_status",
    "propagation_analysis",
    "visual_summary",
    "run_compare",
    "explain_decision",
    "followup",
    "other",
]


class RouterTargets(BaseModel):
    run_id: Optional[str] = None
    topic_id: Optional[str] = None
    claim_id: Optional[str] = None


class RouterOutput(BaseModel):
    intent: RouterIntent = "other"
    targets: RouterTargets = Field(default_factory=RouterTargets)
    confidence: float = 0.0
    fallback_reason: Optional[str] = None


# Phase 3 system prompt — 6 intents + followup + other.
_SYSTEM_PROMPT_PHASE3 = """You are a routing classifier for a social-media analysis system.
Given the latest user message and optional session context, return STRICT JSON:

{
  "intent": one of [
      "topic_overview",          // "what topics", "what's trending", "today's hot topics"
      "emotion_analysis",        // "how's the mood", "which topics are angriest", "sentiment"
      "claim_status",            // "is X true?", "any evidence for X?", "fact check X"
      "propagation_analysis",    // "who is spreading X?", "is this coordinated?", "bridge accounts"
      "visual_summary",          // "show me a card", "summarize in one image", "render the rebuttal"
      "run_compare",             // "compare to last run", "how did things change", "vs yesterday"
      "explain_decision",        // "why did we intervene", "why no counter-message", "explain the decision"
      "followup",                // pronouns ("it", "that one", "this claim") with no new intent
      "other"
  ],
  "targets": {
    "run_id":       string or null,  // e.g. "20260420-041059-6afd7c" or "latest"
    "topic_id":     string or null,
    "claim_id":     string or null   // e.g. "claim_0001"
  },
  "confidence": 0..1,
  "fallback_reason": string or null
}

Rules:
- If the user explicitly names "latest" or doesn't specify, set run_id="latest".
- If the user mentions a specific run_id, preserve its exact string.
- "followup" means reuse the previous capability with session targets; emit
  followup only when the user clearly refers to prior context without stating a new task.
- For run_compare: target run goes in targets.run_id; baseline is inferred.
- If you cannot confidently classify, set intent="other" and include fallback_reason.
- Output valid JSON only. No prose.
"""


class IntentRouter:
    """Minimal LLM-backed router."""

    def __init__(self, max_intents: Optional[list[str]] = None):
        self._client = openai.OpenAI(api_key=OPENAI_API_KEY)
        self._max_intents = max_intents  # for later phases; None = default

    def classify(
        self,
        message: str,
        session_context: Optional[dict] = None,
    ) -> RouterOutput:
        ctx = json.dumps(session_context or {}, ensure_ascii=False)
        user_msg = (
            f"Session context (may be empty): {ctx}\n"
            f"User message: {message}"
        )
        try:
            resp = self._client.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=256,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT_PHASE3},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            return RouterOutput.model_validate(data)
        except Exception as exc:
            return RouterOutput(
                intent="other",
                confidence=0.0,
                fallback_reason=f"router_error: {type(exc).__name__}: {exc}",
            )
