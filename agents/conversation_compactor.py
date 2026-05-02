"""
Conversation compactor - redesign-2026-05 Phase 6 (B).

When a session's `conversation` exceeds SESSION_MAX_TURNS, the oldest
turns are dropped from the in-memory list. Before dropping, this module
compresses them into `state.summary` so the Rewriter still sees what the
user established earlier.

Trigger: `session_store.save()` calls `maybe_compact(state)` whenever the
unsummarised tail is at least `MIN_TURNS_TO_COMPACT` turns long. One LLM
call per compaction; cost amortised across many turns.

Failure mode: when the LLM call errors, the compactor logs and returns
without modifying state. Worst case the trim runs without a summary,
which loses pre-window context but never corrupts the session.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.session import ConversationTurn, SessionState

log = structlog.get_logger(__name__)


SESSION_MAX_TURNS: int = int(os.getenv("SESSION_MAX_TURNS", "40"))
MIN_TURNS_TO_COMPACT: int = int(os.getenv("SESSION_MIN_TURNS_TO_COMPACT", "10"))
SUMMARY_MAX_CHARS: int = int(os.getenv("SESSION_SUMMARY_MAX_CHARS", "1200"))


def _build_system_prompt(char_limit: int) -> str:
    return (
        "You are a conversation compactor for a research assistant.\n"
        "You will be given:\n"
        "  - the existing rolling summary of older turns (may be empty)\n"
        "  - a chunk of newer turns (user / assistant exchanges) that need\n"
        "    to be folded into the summary\n\n"
        'Output STRICT JSON: {"summary": "<merged summary>"}.\n\n'
        "Rules:\n"
        "- Keep facts the user established (which topic / run / claim,\n"
        "  what they asked, what the assistant answered factually, any\n"
        "  user preferences).\n"
        "- Drop chit-chat, greetings, repeated questions.\n"
        "- Preserve concrete identifiers (topic_id, claim_id, run_id,\n"
        "  account names) verbatim.\n"
        f"- Be concise. Aim for under {char_limit} characters total.\n"
        "- Use third-person bullet points. No prose paragraphs.\n"
        "- The output replaces the previous summary entirely; include\n"
        "  all still-relevant facts.\n"
    )


def maybe_compact(
    state: SessionState,
    *,
    max_turns: int = SESSION_MAX_TURNS,
    min_to_compact: int = MIN_TURNS_TO_COMPACT,
    client: Optional[openai.OpenAI] = None,
) -> bool:
    """Compact + trim if conversation exceeds the configured window.

    Returns True iff state was modified.
    """
    if len(state.conversation) <= max_turns:
        return False

    excess = len(state.conversation) - max_turns
    # Only compact when we actually have enough turns to amortise the
    # LLM call cost; below this we just drop silently.
    drop_count = max(excess, min_to_compact)
    drop_count = min(drop_count, len(state.conversation))

    to_drop = state.conversation[:drop_count]
    keep = state.conversation[drop_count:]

    new_summary = _llm_compact(
        prior_summary=state.summary,
        turns=to_drop,
        client=client,
    )
    if new_summary is None:
        # LLM unreachable: drop anyway (path A semantics) but log.
        log.warning("compactor.llm_failed_drop_only",
                    session_id=state.session_id, drop_count=drop_count)

    state.conversation = keep
    state.archived_count += drop_count
    state.summary_until_turn = state.archived_count
    if new_summary:
        state.summary = new_summary[:SUMMARY_MAX_CHARS]

    log.info("compactor.compacted",
             session_id=state.session_id,
             dropped=drop_count,
             kept=len(state.conversation),
             summary_chars=len(state.summary),
             total_turns_seen=state.total_turns_seen())
    return True


def _llm_compact(
    prior_summary: str,
    turns: list[ConversationTurn],
    client: Optional[openai.OpenAI] = None,
) -> Optional[str]:
    if not turns:
        return prior_summary or ""
    cli = client or openai.OpenAI(api_key=OPENAI_API_KEY)

    body_lines: list[str] = []
    if prior_summary:
        body_lines.append("Previous summary:")
        body_lines.append(prior_summary[:SUMMARY_MAX_CHARS * 2])
        body_lines.append("")
    body_lines.append("New turns to fold in:")
    for t in turns:
        # Keep payload bounded: user content full, assistant content
        # truncated since it's already been distilled by the writer.
        cap = 600 if t.role == "user" else 240
        text = (t.content or "").strip().replace("\n", " ")
        body_lines.append(
            f"  [{t.role}]"
            f"{(' branches=' + ','.join(t.branches_used)) if t.branches_used else ''}: "
            f"{text[:cap]}"
        )

    user_msg = "\n".join(body_lines)
    system_msg = _build_system_prompt(SUMMARY_MAX_CHARS)

    try:
        resp = cli.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=400,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg[:8000]},
            ],
        )
        raw = (resp.choices[0].message.content or "{}").strip()
        data = json.loads(raw)
        out = (data.get("summary") or "").strip()
        return out or prior_summary or None
    except Exception as exc:
        log.warning("compactor.llm_error", error=str(exc)[:160])
        return None
