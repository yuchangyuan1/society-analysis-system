"""Chat page — three-column conversational workspace (ui_design_plan §11 MVP).

Layout
------
    [ st.sidebar ]  [ Chat column ]  [ Analysis tabs column ]
       context         message         5 tabs reading from
       + prompts       stream          st.session_state["panel_*"]

Each chat turn calls the same `chat_query` endpoint; the capability output
is (a) rendered inline under the assistant message via the existing
`render_capability_output` helper, and (b) mirrored to the right-side
panels via `analysis_tabs.route_capability_to_panels`, so the analysis
workspace always reflects the latest answer.
"""
from __future__ import annotations

import uuid
from typing import Optional

import streamlit as st

from ui import api_client
from ui.components import analysis_tabs
from ui.components.chat_response import render_capability_output


st.set_page_config(page_title="Chat · Society Analysis", layout="wide")

# ─── Session state defaults ──────────────────────────────────────────────────

if "session_id" not in st.session_state:
    st.session_state.session_id = f"s-{uuid.uuid4().hex[:12]}"
if "history" not in st.session_state:
    st.session_state.history: list[dict] = []
if "pending_prompt" not in st.session_state:
    st.session_state.pending_prompt: Optional[str] = None
if "breadcrumb" not in st.session_state:
    st.session_state.breadcrumb: dict = {}


_PROMPT_CHIPS = [
    "What topics are trending?",
    "How is the emotional tone?",
    "Summarise this run in one card",
    "Compare with the previous run",
    "Why didn't we intervene?",
]


# ─── Sidebar: session context + prompts ──────────────────────────────────────

def _render_sidebar() -> None:
    with st.sidebar:
        st.subheader("Session")
        st.code(st.session_state.session_id, language=None)

        bc = st.session_state.breadcrumb or {}
        run_id = bc.get("current_run_id")
        topic_id = bc.get("current_topic_id")
        claim_id = bc.get("current_claim_id")
        st.markdown(
            f"- **run:** `{run_id or '—'}`  \n"
            f"- **topic:** `{topic_id or '—'}`  \n"
            f"- **claim:** `{claim_id or '—'}`"
        )

        if st.button("New session", use_container_width=True):
            st.session_state.session_id = f"s-{uuid.uuid4().hex[:12]}"
            st.session_state.history = []
            st.session_state.breadcrumb = {}
            analysis_tabs.reset_panels()
            st.rerun()

        st.divider()
        st.subheader("Try asking")
        for chip in _PROMPT_CHIPS:
            if st.button(chip, key=f"chip::{chip}", use_container_width=True):
                st.session_state.pending_prompt = chip
                st.rerun()

        st.divider()
        try:
            h = api_client.health()
            st.caption(f"API OK · runs_root `{h.get('runs_root')}`")
        except Exception as exc:  # noqa: BLE001
            st.caption(f":red[API unreachable]: {exc}")


# ─── Main panes ──────────────────────────────────────────────────────────────

def _render_chat_history() -> None:
    for turn in st.session_state.history:
        role = turn["role"]
        with st.chat_message(role):
            st.markdown(turn["content"])
            if role == "assistant" and turn.get("capability_output"):
                with st.expander("Structured output", expanded=False):
                    render_capability_output(
                        turn.get("capability_used"),
                        turn["capability_output"],
                    )


def _process_turn(prompt: str) -> None:
    """Send a user message, record assistant reply, update panels."""
    st.session_state.history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Thinking…_")
        try:
            resp = api_client.chat_query(
                session_id=st.session_state.session_id,
                message=prompt,
            )
        except Exception as exc:  # noqa: BLE001
            placeholder.error(
                f"Chat call failed: {exc}\n"
                f"API base: `{api_client.API_BASE}`.\n"
                "Is uvicorn running on port 8000?"
            )
            st.session_state.history.append(
                {"role": "assistant", "content": f"*Error: {exc}*"}
            )
            return

        answer = resp.get("answer_text") or ""
        placeholder.markdown(answer)
        cap_name = resp.get("capability_used")
        cap_output = resp.get("capability_output") or {}

        if cap_output:
            with st.expander("Structured output", expanded=False):
                render_capability_output(cap_name, cap_output)

        st.session_state.history.append(
            {
                "role": "assistant",
                "content": answer,
                "capability_used": cap_name,
                "capability_output": cap_output,
            }
        )

        analysis_tabs.route_capability_to_panels(cap_name, cap_output)

    _refresh_breadcrumb()


def _refresh_breadcrumb() -> None:
    """Pull current_* fields from the server session so the sidebar is in sync."""
    try:
        sess = api_client.get_session(st.session_state.session_id)
    except Exception:  # noqa: BLE001
        return
    st.session_state.breadcrumb = {
        "current_run_id": sess.get("current_run_id"),
        "current_topic_id": sess.get("current_topic_id"),
        "current_claim_id": sess.get("current_claim_id"),
    }


# ─── Layout ──────────────────────────────────────────────────────────────────

_render_sidebar()

st.title("Chat — Society Analysis Assistant")
st.caption(
    "Left: session · Middle: conversation · Right: evidence, propagation, "
    "metrics, and visual cards drawn from the latest answer."
)

chat_col, analysis_col = st.columns([3, 2], gap="large")

with chat_col:
    st.markdown("### Conversation")
    _render_chat_history()

    pending = st.session_state.pending_prompt
    if pending:
        st.session_state.pending_prompt = None
        _process_turn(pending)

    prompt = st.chat_input("Ask about a run…")
    if prompt:
        _process_turn(prompt)

with analysis_col:
    st.markdown("### Analysis workspace")
    analysis_tabs.render()
