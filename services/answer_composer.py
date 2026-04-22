"""Answer Composer — turn a capability output into human-readable text.

Rules (from interactive_agent_transformation_plan_skills_mcp.md §15):
- Does NOT introduce new reasoning or new claims.
- Just translates the structured capability output into natural language.
- When LLM call fails, falls back to a deterministic template.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import openai
from config import OPENAI_API_KEY, OPENAI_MODEL


_SYSTEM_PROMPT = """You are a concise assistant for a social-media analysis system.
You will receive:
  - the user's question,
  - the name of the capability that was executed,
  - its structured output (JSON).

Write a SHORT answer (3–6 sentences, Chinese or English matching the user's
language) that faithfully summarises the structured output.

Hard rules:
- Do NOT invent facts not present in the JSON.
- Do NOT add caveats, disclaimers, or recommendations.
- Prefer bullet lists when the output lists items (topics, claims).
- Keep numbers faithful (round to 2 decimals where helpful).
"""


class AnswerComposer:
    def __init__(self) -> None:
        self._client = openai.OpenAI(api_key=OPENAI_API_KEY)

    def compose(
        self,
        user_message: str,
        capability_name: Optional[str],
        capability_output: Optional[dict[str, Any]],
        session_context: Optional[dict] = None,
    ) -> str:
        if capability_output is None:
            return self._fallback_no_capability(user_message)

        payload = {
            "user_message": user_message,
            "capability": capability_name,
            "capability_output": capability_output,
            "session_context": session_context or {},
        }
        try:
            resp = self._client.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=400,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception:
            return self._fallback_template(capability_name, capability_output)

    # ── Fallbacks ──────────────────────────────────────────────────────────

    @staticmethod
    def _fallback_no_capability(user_message: str) -> str:
        return (
            "I could not confidently route your question. "
            "Try asking about topics (\"what's being discussed?\"), "
            "emotions, claims, or propagation."
        )

    @staticmethod
    def _fallback_template(
        capability_name: Optional[str],
        capability_output: dict[str, Any],
    ) -> str:
        if capability_name == "topic_overview":
            topics = capability_output.get("topics") or []
            lines = [f"Run {capability_output.get('run_id', '?')} — top topics:"]
            for i, t in enumerate(topics[:5], 1):
                lines.append(
                    f"  {i}. {t.get('label','?')} — "
                    f"posts={t.get('post_count', 0)}, "
                    f"emotion={t.get('dominant_emotion','')}"
                )
            return "\n".join(lines)
        if capability_name == "emotion_analysis":
            d = capability_output.get("overall_emotion_distribution") or {}
            dom = capability_output.get("dominant_emotion") or "?"
            lines = [f"Dominant emotion: {dom}"]
            for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:4]:
                lines.append(f"  - {k}: {v:.2f}")
            interp = capability_output.get("interpretation")
            if interp:
                lines.append(interp)
            return "\n".join(lines)
        if capability_name == "claim_status":
            verdict = capability_output.get("verdict_label", "?")
            text = capability_output.get("claim_text", "")
            sup = capability_output.get("supporting_count", 0)
            con = capability_output.get("contradicting_count", 0)
            unc = capability_output.get("uncertain_count", 0)
            return (
                f"Claim: \"{text}\"\n"
                f"Verdict: {verdict}\n"
                f"Evidence — supporting: {sup}, contradicting: {con}, "
                f"uncertain: {unc}"
            )
        if capability_name == "visual_summary":
            status = capability_output.get("status", "?")
            expl = capability_output.get("explanation", "")
            if status == "rendered":
                return f"Rendered card for claim \"{capability_output.get('claim_text','')}\". {expl}".strip()
            if status == "abstained":
                return f"No card — abstained. {expl}".strip()
            if status == "no_decision":
                return expl or "This run has no intervention decision."
            if status == "insufficient_data":
                return expl or "Not enough data to render this card."
            return f"Card render failed: {capability_output.get('reason', expl)}"
        if capability_name == "run_compare":
            target = capability_output.get("target_run_id", "?")
            baseline = capability_output.get("baseline_run_id", "?")
            lines = [f"Comparing {target} vs {baseline}:"]
            for ch in (capability_output.get("changes") or [])[:8]:
                arrow = {"up": "↑", "down": "↓", "flat": "→", "unknown": "·"}[
                    ch.get("direction", "unknown")
                ]
                lines.append(
                    f"  {arrow} {ch.get('field','?')}: "
                    f"{ch.get('baseline')} → {ch.get('target')} "
                    f"(Δ {ch.get('delta')})"
                )
            return "\n".join(lines)
        if capability_name == "explain_decision":
            dec = capability_output.get("decision") or {}
            if not dec:
                return "No intervention decision available for this run."
            lines = [
                f"Decision: {dec.get('decision','?')}",
                f"Explanation: {dec.get('explanation','')}",
            ]
            if dec.get("recommended_next_step"):
                lines.append(f"Next step: {dec['recommended_next_step']}")
            skip = capability_output.get("counter_message_skip_reason")
            if skip:
                lines.append(f"Skip reason: {skip}")
            hist = capability_output.get("history") or []
            if hist:
                lines.append(f"Prior deployments on this topic: {len(hist)}")
            return "\n".join(lines)
        if capability_name == "propagation_analysis":
            lines = [
                f"Posts: {capability_output.get('post_count', 0)}, "
                f"accounts: {capability_output.get('unique_accounts', 0)}, "
                f"velocity: {capability_output.get('velocity', 0.0):.2f}/h",
                f"Communities: {capability_output.get('community_count', 0)} "
                f"(echo chambers: {capability_output.get('echo_chamber_count', 0)})",
                f"Coordinated pairs: {capability_output.get('coordinated_pairs', 0)}, "
                f"bridge influence: "
                f"{capability_output.get('bridge_influence_ratio', 0.0):.2f}",
            ]
            roles = capability_output.get("account_role_summary") or {}
            if roles:
                lines.append("Roles: " + ", ".join(
                    f"{k}={v}" for k, v in roles.items()
                ))
            return "\n".join(lines)
        return json.dumps(capability_output, ensure_ascii=False)[:500]
