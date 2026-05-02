"""
Phase 6 (A + B) - session context optimisation tests.

Coverage:
- maybe_compact: no-op when conversation fits inside the window
- maybe_compact: trims + summarises when conversation overflows
- maybe_compact: LLM failure falls back to drop-only (path A)
- summary_until_turn / archived_count bookkeeping
- QueryRewriter._session_context surfaces older_context_summary
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from agents.conversation_compactor import maybe_compact
from agents.query_rewriter import QueryRewriter
from models.session import ConversationTurn, SessionState


def _mock_openai(payload: dict) -> MagicMock:
    msg = MagicMock()
    msg.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def _make_state(turns: int) -> SessionState:
    state = SessionState(session_id="s1")
    for i in range(turns):
        state.conversation.append(ConversationTurn(
            role="user" if i % 2 == 0 else "assistant",
            content=f"turn-{i} content",
            branches_used=["nl2sql"] if i % 2 else [],
        ))
    return state


def test_compactor_no_op_when_within_window():
    state = _make_state(10)
    client = MagicMock()
    changed = maybe_compact(state, max_turns=40, min_to_compact=5,
                              client=client)
    assert changed is False
    assert len(state.conversation) == 10
    assert state.archived_count == 0
    assert state.summary == ""
    client.chat.completions.create.assert_not_called()


def test_compactor_trims_and_summarises_when_overflow():
    state = _make_state(50)
    client = _mock_openai({"summary": "User asked about vaccines and topics."})
    changed = maybe_compact(state, max_turns=40, min_to_compact=10,
                              client=client)
    assert changed is True
    # 50 - 40 = 10 excess but min_to_compact=10 -> drop 10 turns.
    assert len(state.conversation) == 40
    assert state.archived_count == 10
    assert state.summary_until_turn == 10
    assert "vaccines" in state.summary


def test_compactor_drops_at_least_min_to_compact():
    """Even small overflows trigger drop_count >= min_to_compact."""
    state = _make_state(42)  # 2 over limit
    client = _mock_openai({"summary": "S"})
    maybe_compact(state, max_turns=40, min_to_compact=8, client=client)
    # excess=2 but min=8 -> drop 8
    assert state.archived_count == 8
    assert len(state.conversation) == 34


def test_compactor_llm_failure_falls_back_to_drop_only():
    state = _make_state(50)
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("api down")
    changed = maybe_compact(state, max_turns=40, min_to_compact=10,
                              client=client)
    assert changed is True
    # Trim still happened; summary stays empty (path A semantics).
    assert state.archived_count == 10
    assert state.summary == ""


def test_compactor_appends_to_existing_summary():
    state = _make_state(50)
    state.summary = "Previously: user asked about climate."
    state.archived_count = 5  # pretend 5 prior dropped
    client = _mock_openai({
        "summary": "Older: climate. Newer: vaccines and BBC reporting.",
    })
    maybe_compact(state, max_turns=40, min_to_compact=10, client=client)
    assert state.archived_count == 15  # 5 + 10
    assert "climate" in state.summary
    assert "vaccines" in state.summary
    # The compactor should have included the old summary in the prompt
    sent = client.chat.completions.create.call_args.kwargs["messages"]
    user_payload = sent[1]["content"]
    assert "Previously: user asked about climate" in user_payload


def test_total_turns_seen():
    state = _make_state(5)
    state.archived_count = 100
    assert state.total_turns_seen() == 105


def test_rewriter_context_includes_summary_when_present():
    state = SessionState(
        session_id="s1",
        current_topic_id="topic_xxx",
        summary="User established interest in vaccine misinformation.",
        archived_count=15,
    )
    state.conversation.append(ConversationTurn(role="user", content="hi"))
    ctx = QueryRewriter._session_context(state)
    assert ctx["older_context_summary"].startswith("User established")
    assert ctx["archived_turns"] == 15


def test_rewriter_context_omits_summary_when_empty():
    state = SessionState(session_id="s1", current_topic_id="t1")
    ctx = QueryRewriter._session_context(state)
    assert "older_context_summary" not in ctx
    assert "archived_turns" not in ctx
