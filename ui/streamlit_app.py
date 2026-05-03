"""Single-page Streamlit interface for the Society Analysis demo."""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Optional

import streamlit as st

from ui import api_client
from ui.components.chat_response import (
    render_debug_outputs,
    render_knowledge_graph,
    render_route_modules,
    render_source_list,
)


st.set_page_config(page_title="Society Analysis", layout="wide")


_SUBREDDIT_OPTIONS = [
    "worldnews",
    "news",
    "politics",
    "geopolitics",
    "health",
    "technology",
]
_OFFICIAL_SOURCE_OPTIONS = ["ap", "reuters", "bbc", "nyt", "xinhua"]

_QUESTION_TEMPLATES = [
    (
        "Topics overview",
        "List the main topics in the selected Reddit data, and summarize "
        "discussion volume, dominant emotion, and notable shifts.",
    ),
    (
        "Emotion analysis",
        "For the topic about <your topic>, summarize the dominant emotions, "
        "representative posts, and how sentiment differs across discussion clusters.",
    ),
    (
        "Propagation paths",
        "For the topic about <your topic>, trace propagation paths or reply "
        "chains and explain what the Knowledge Graph shows.",
    ),
    (
        "Amplifying accounts",
        "For the topic about <your topic>, identify key amplifying accounts "
        "and explain the graph evidence behind the ranking.",
    ),
    (
        "Claim verification",
        "For the topic about <your topic>, list Reddit claims and classify "
        "which are consistent with official/evidence sources, which contradict "
        "them, and which lack enough evidence. Include author, verdict, the "
        "official/evidence statement, and citation.",
    ),
    (
        "Single claim check",
        "Fact-check this Reddit claim using the selected official/evidence "
        "sources: <paste claim here>.",
    ),
]


def _init_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = f"demo-{uuid.uuid4().hex[:10]}"
    if "history" not in st.session_state:
        st.session_state.history: list[dict[str, Any]] = []
    if "draft_prompt" not in st.session_state:
        st.session_state.draft_prompt: Optional[str] = None
    if "debug_mode" not in st.session_state:
        st.session_state.debug_mode = False
    if "import_jobs" not in st.session_state:
        st.session_state.import_jobs: list[str] = []


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            max-width: 1220px;
            padding-top: 1.45rem;
            padding-bottom: 3rem;
        }
        [data-testid="stSidebar"] {
            background: #f7f8fa;
            border-right: 1px solid #e5e7eb;
        }
        div[data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 0.7rem 0.75rem;
        }
        .app-header {
            border-bottom: 1px solid #e5e7eb;
            padding-bottom: 0.95rem;
            margin-bottom: 1rem;
        }
        .app-header h1 {
            font-size: 1.85rem;
            line-height: 1.15;
            margin: 0 0 0.35rem 0;
        }
        .app-header p {
            color: #4b5563;
            margin: 0;
            max-width: 820px;
        }
        .section-label {
            color: #374151;
            font-weight: 700;
            margin: 0.4rem 0 0.25rem 0;
        }
        .muted-note {
            color: #6b7280;
            font-size: 0.86rem;
        }
        .stButton button {
            border-radius: 7px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _api_status() -> bool:
    try:
        api_client.health()
        return True
    except Exception:
        return False


def _date_range_value(value: Any) -> tuple[date, date]:
    if isinstance(value, tuple) and len(value) == 2:
        return value
    if isinstance(value, list) and len(value) == 2:
        return value[0], value[1]
    today = date.today()
    return today - timedelta(days=1), today


def _render_source_controls() -> dict[str, Any]:
    st.sidebar.markdown("### Data Sources")
    subreddits = st.sidebar.multiselect(
        "Reddit subreddits",
        _SUBREDDIT_OPTIONS,
        default=["worldnews"],
        help="Select which Reddit communities the agent should focus on.",
    )
    reddit_range = st.sidebar.date_input(
        "Reddit crawl date range",
        value=(date.today() - timedelta(days=1), date.today()),
    )

    official_sources = st.sidebar.multiselect(
        "Official/evidence sources",
        _OFFICIAL_SOURCE_OPTIONS,
        default=["ap", "reuters", "bbc", "nyt"],
        help="Select evidence sources for RAG verification.",
    )
    official_range = st.sidebar.date_input(
        "Official source date range",
        value=(date.today() - timedelta(days=1), date.today()),
    )

    mode_label = st.sidebar.radio(
        "Import mode",
        ["Append new data", "Overwrite retained data"],
        horizontal=False,
    )
    mode = "overwrite" if mode_label.startswith("Overwrite") else "append"
    confirm_overwrite = False
    if mode == "overwrite":
        st.sidebar.warning(
            "Overwrite deletes retained data for the selected source scope before import."
        )
        confirm_overwrite = st.sidebar.checkbox(
            "Confirm overwrite before importing",
            value=False,
        )

    return {
        "subreddits": subreddits or ["worldnews"],
        "reddit_range": _date_range_value(reddit_range),
        "official_sources": official_sources or _OFFICIAL_SOURCE_OPTIONS,
        "official_range": _date_range_value(official_range),
        "mode": mode,
        "confirm_overwrite": confirm_overwrite,
    }


def _render_import_buttons(config: dict[str, Any]) -> None:
    reddit_start, reddit_end = config["reddit_range"]
    official_start, official_end = config["official_range"]
    col_a, col_b = st.sidebar.columns(2)
    with col_a:
        if st.button("Import Reddit", use_container_width=True):
            if config["mode"] == "overwrite" and not config["confirm_overwrite"]:
                st.sidebar.error("Confirm overwrite before importing Reddit data.")
                return
            payload = {
                "subreddits": config["subreddits"],
                "start_date": reddit_start.isoformat(),
                "end_date": reddit_end.isoformat(),
                "mode": config["mode"],
                "confirm_overwrite": config["confirm_overwrite"],
                "limit_per_subreddit": 100,
                "comment_limit": 100,
                "include_comments": True,
            }
            try:
                job = api_client.import_reddit(payload)
                st.session_state.import_jobs.insert(0, job["job_id"])
                st.sidebar.success(f"Reddit import queued: {job['job_id']}")
            except Exception as exc:
                st.sidebar.error(f"Could not queue Reddit import: {exc}")
    with col_b:
        if st.button("Import Official", use_container_width=True):
            if config["mode"] == "overwrite" and not config["confirm_overwrite"]:
                st.sidebar.error("Confirm overwrite before importing official data.")
                return
            payload = {
                "sources": config["official_sources"],
                "start_date": official_start.isoformat(),
                "end_date": official_end.isoformat(),
                "mode": config["mode"],
                "confirm_overwrite": config["confirm_overwrite"],
                "write_chroma": True,
            }
            try:
                job = api_client.import_official(payload)
                st.session_state.import_jobs.insert(0, job["job_id"])
                st.sidebar.success(f"Official import queued: {job['job_id']}")
            except Exception as exc:
                st.sidebar.error(f"Could not queue official import: {exc}")

    if st.session_state.import_jobs:
        with st.sidebar.expander("Import Jobs", expanded=False):
            for job_id in st.session_state.import_jobs[:5]:
                try:
                    job = api_client.get_import_job(job_id)
                    status = job.get("status", "unknown")
                    st.caption(f"{job_id}: {status}")
                    if job.get("error"):
                        st.error(job["error"])
                    for warning in job.get("warnings") or []:
                        st.warning(warning)
                except Exception:
                    st.caption(f"{job_id}: status unavailable")


def _render_sidebar() -> dict[str, Any]:
    with st.sidebar:
        st.markdown("## Society Analysis")
        st.caption("Single-page research demo")
        api_ok = _api_status()
        st.status("API connected" if api_ok else "API unavailable",
                  state="complete" if api_ok else "error")

        if st.button("New chat session", use_container_width=True):
            st.session_state.session_id = f"demo-{uuid.uuid4().hex[:10]}"
            st.session_state.history = []
            st.session_state.draft_prompt = None
            st.rerun()

        st.toggle(
            "Show raw technical output",
            key="debug_mode",
            help="Show raw SQL, KG, and RAG payloads for debugging.",
        )

        st.divider()
        config = _render_source_controls()
        _render_import_buttons(config)

        st.divider()
        st.markdown("### Suggested Questions")
        for label, prompt in _QUESTION_TEMPLATES:
            if st.button(label, use_container_width=True):
                st.session_state.draft_prompt = prompt
                st.rerun()

    return config


def _render_header(config: dict[str, Any]) -> None:
    reddit_start, reddit_end = config["reddit_range"]
    official_start, official_end = config["official_range"]
    st.markdown(
        """
        <div class="app-header">
          <h1>Society Analysis Assistant</h1>
          <p>
            Analyze Reddit discussions, inspect Knowledge Graph propagation,
            query structured topic data, and verify Reddit claims against
            official/evidence sources.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    cols = st.columns(4)
    cols[0].metric("Reddit", ", ".join(config["subreddits"]))
    cols[1].metric("Evidence", f"{len(config['official_sources'])} sources")
    cols[2].metric("Reddit range", f"{reddit_start:%m/%d} - {reddit_end:%m/%d}")
    cols[3].metric("Evidence range", f"{official_start:%m/%d} - {official_end:%m/%d}")


def _filter_context(config: dict[str, Any]) -> str:
    reddit_start, reddit_end = config["reddit_range"]
    official_start, official_end = config["official_range"]
    return (
        "\n\nUI data-source filters for this turn:\n"
        f"- Reddit subreddits: {', '.join(config['subreddits'])}\n"
        f"- Reddit date range: {reddit_start.isoformat()} to {reddit_end.isoformat()}\n"
        f"- Official/evidence sources: {', '.join(config['official_sources'])}\n"
        f"- Official/evidence date range: {official_start.isoformat()} to {official_end.isoformat()}\n"
        "Use these filters when selecting data, SQL constraints, and evidence retrieval."
    )


def _render_assistant_payload(turn: dict[str, Any]) -> None:
    render_source_list(turn.get("citations") or [])
    render_route_modules(
        turn.get("branches_used") or [],
        turn.get("branch_outputs") or {},
    )
    render_knowledge_graph(turn.get("branch_outputs") or {})
    if st.session_state.debug_mode:
        render_debug_outputs(
            turn.get("branches_used") or [],
            turn.get("branch_outputs") or {},
            turn.get("capability_used"),
            turn.get("capability_output"),
        )


def _render_history() -> None:
    if not st.session_state.history:
        st.info(
            "Choose a suggested question or write your own. Suggested questions "
            "are templates; replace the placeholder topic or claim with the case "
            "you want to test."
        )
        return

    for turn in st.session_state.history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn["role"] == "assistant":
                _render_assistant_payload(turn)


def _process_turn(prompt: str, config: dict[str, Any]) -> None:
    visible_prompt = prompt.strip()
    if not visible_prompt:
        return
    st.session_state.history.append({"role": "user", "content": visible_prompt})
    with st.chat_message("user"):
        st.markdown(visible_prompt)

    with st.chat_message("assistant"):
        with st.spinner("Routing through RAG, Knowledge Graph, and NL2SQL as needed..."):
            try:
                resp = api_client.chat_query(
                    session_id=st.session_state.session_id,
                    message=visible_prompt + _filter_context(config),
                )
            except Exception as exc:
                st.error(
                    f"Could not reach the analysis API at `{api_client.API_BASE}`. "
                    f"Details: {exc}"
                )
                st.session_state.history.append({
                    "role": "assistant",
                    "content": f"API error: {exc}",
                })
                return

        answer = resp.get("answer_text") or "No answer returned."
        st.markdown(answer)

        assistant_turn = {
            "role": "assistant",
            "content": answer,
            "citations": resp.get("citations") or [],
            "branches_used": resp.get("branches_used") or [],
            "branch_outputs": resp.get("branch_outputs") or {},
            "capability_used": resp.get("capability_used"),
            "capability_output": resp.get("capability_output") or {},
        }
        _render_assistant_payload(assistant_turn)
        st.session_state.history.append(assistant_turn)


def _render_composer(config: dict[str, Any]) -> None:
    draft = st.session_state.draft_prompt
    if draft:
        st.markdown('<div class="section-label">Draft question</div>',
                    unsafe_allow_html=True)
        edited = st.text_area(
            "Draft question",
            value=draft,
            height=120,
            label_visibility="collapsed",
        )
        cols = st.columns([1, 1, 5])
        if cols[0].button("Ask", type="primary", use_container_width=True):
            st.session_state.draft_prompt = None
            _process_turn(edited, config)
        if cols[1].button("Clear", use_container_width=True):
            st.session_state.draft_prompt = None
            st.rerun()

    prompt = st.chat_input("Ask about topics, claims, emotions, propagation, or amplifying accounts")
    if prompt:
        _process_turn(prompt, config)


_init_state()
_inject_css()
source_config = _render_sidebar()
_render_header(source_config)
_render_history()
_render_composer(source_config)
