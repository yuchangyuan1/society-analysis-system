"""Right-side analysis workspace — 5 tabs backed by session state.

Each capability response is mirrored into one or more panels
(`st.session_state["panel_<name>"]`) via `route_capability_to_panels()`.
`render()` draws all five tabs every rerun; tabs without state show a
short hint so the UI never looks broken in empty states.

Panels:
    panel_evidence  — latest claim_status output
    panel_topic     — latest topic_overview / emotion_analysis /
                      claim_status / explain_decision
    panel_graph     — latest propagation_analysis
    panel_metrics   — latest run_compare or propagation_analysis
    panel_visual    — latest visual_summary / explain_decision (if card)
"""
from __future__ import annotations

from typing import Any, Optional

import streamlit as st


_PANEL_KEYS = (
    "panel_evidence",
    "panel_topic",
    "panel_graph",
    "panel_metrics",
    "panel_visual",
)


def reset_panels() -> None:
    """Clear every right-side panel. Called on New Session."""
    for k in _PANEL_KEYS:
        st.session_state.pop(k, None)


def route_branches_to_panels(
    branches_used: list[str], branch_outputs: dict[str, Any],
) -> None:
    """redesign-2026-05 Phase 4.6: route v2 branch outputs into panels.

    Mapping (PROJECT_REDESIGN_V2.md UI section, adjusted for v2 branches):
        evidence -> Evidence panel
        nl2sql   -> Topic + Metrics panels (whichever has rows)
        kg       -> Graph panel
    """
    if not branches_used and not branch_outputs:
        return
    if "evidence" in branch_outputs and branch_outputs["evidence"]:
        st.session_state["panel_evidence"] = {
            "source": "evidence",
            "data": branch_outputs["evidence"][0],
        }
    if "nl2sql" in branch_outputs and branch_outputs["nl2sql"]:
        st.session_state["panel_topic"] = {
            "source": "nl2sql",
            "data": branch_outputs["nl2sql"][0],
        }
        st.session_state["panel_metrics"] = {
            "source": "nl2sql",
            "data": branch_outputs["nl2sql"][0],
        }
    if "kg" in branch_outputs and branch_outputs["kg"]:
        st.session_state["panel_graph"] = {
            "source": "kg",
            "data": branch_outputs["kg"][0],
        }


def route_capability_to_panels(
    capability_name: Optional[str], output: dict[str, Any]
) -> None:
    """Store capability output under the panel(s) it feeds.

    The planner-DAG path may attach secondary step outputs under
    `output["aux_outputs"]` (see agents/planner.py). We route those too so a
    `claim_verification_flow` can populate both the Evidence tab (primary)
    and the Graph tab (from its `propagation_context` step).

    DEPRECATED for the v2 chat path: prefer route_branches_to_panels.
    Kept as fallback when only the legacy `capability_used` field is set.
    """
    if not output or "error" in output:
        return

    name = capability_name or ""
    if name == "topic_overview":
        st.session_state["panel_topic"] = {"source": name, "data": output}
    elif name == "emotion_analysis":
        st.session_state["panel_topic"] = {"source": name, "data": output}
    elif name == "claim_status":
        st.session_state["panel_evidence"] = {"source": name, "data": output}
        st.session_state["panel_topic"] = {"source": name, "data": output}
    elif name == "propagation_analysis":
        st.session_state["panel_graph"] = {"source": name, "data": output}
        st.session_state["panel_metrics"] = {"source": name, "data": output}
    elif name == "visual_summary":
        st.session_state["panel_visual"] = {"source": name, "data": output}
    elif name == "run_compare":
        st.session_state["panel_metrics"] = {"source": name, "data": output}
    elif name == "explain_decision":
        st.session_state["panel_topic"] = {"source": name, "data": output}
        if output.get("visual_card_path"):
            st.session_state["panel_visual"] = {"source": name, "data": output}

    # Fan-out aux DAG step outputs to whichever panel each one feeds.
    aux = output.get("aux_outputs") or {}
    for alias, sub in aux.items():
        if not isinstance(sub, dict):
            continue
        if alias == "propagation_analysis" or alias == "propagation_context":
            st.session_state["panel_graph"] = {
                "source": "propagation_analysis", "data": sub,
            }
        elif alias == "visual_summary":
            st.session_state["panel_visual"] = {
                "source": "visual_summary", "data": sub,
            }
        elif alias == "run_compare":
            st.session_state["panel_metrics"] = {
                "source": "run_compare", "data": sub,
            }
        elif alias == "emotion_analysis" and "panel_topic" not in st.session_state:
            st.session_state["panel_topic"] = {
                "source": "emotion_analysis", "data": sub,
            }


def render() -> None:
    """Render the 5-tab analysis workspace."""
    tabs = st.tabs(["Evidence", "Topic", "Graph", "Metrics", "Visual"])
    with tabs[0]:
        _render_evidence(st.session_state.get("panel_evidence"))
    with tabs[1]:
        _render_topic(st.session_state.get("panel_topic"))
    with tabs[2]:
        _render_graph(st.session_state.get("panel_graph"))
    with tabs[3]:
        _render_metrics(st.session_state.get("panel_metrics"))
    with tabs[4]:
        _render_visual(st.session_state.get("panel_visual"))


# ─── Evidence ────────────────────────────────────────────────────────────────

_VERDICT_BADGE = {
    "supported": ":green[supported]",
    "contradicted": ":red[contradicted]",
    "disputed": ":orange[disputed]",
    "insufficient": ":gray[insufficient]",
    "non_factual": ":gray[non-factual]",
}


def _render_evidence(entry: Optional[dict[str, Any]]) -> None:
    if not entry:
        st.caption(
            "No evidence loaded yet. Ask about a specific claim "
            "(e.g. *\"is X true?\"*) to populate this tab."
        )
        return
    data = entry["data"]
    st.markdown(f"**Claim:** _{data.get('claim_text','')}_")
    verdict = data.get("verdict_label", "?")
    st.markdown(f"**Verdict:** {_VERDICT_BADGE.get(verdict, verdict)}")

    rationale = data.get("verdict_rationale")
    if rationale:
        st.caption(rationale)

    st.dataframe(
        [
            {"stance": "supporting", "count": data.get("supporting_count", 0)},
            {"stance": "contradicting", "count": data.get("contradicting_count", 0)},
            {"stance": "uncertain", "count": data.get("uncertain_count", 0)},
        ],
        use_container_width=True,
        hide_index=True,
    )

    for key, title in (
        ("top_supporting", "Top supporting"),
        ("top_contradicting", "Top contradicting"),
    ):
        items = data.get(key) or []
        if not items:
            continue
        st.markdown(f"**{title}** ({len(items)})")
        for ev in items:
            ttl = ev.get("article_title") or ev.get("article_id") or "?"
            url = ev.get("article_url") or ""
            src = ev.get("source_name") or ""
            tier = ev.get("source_tier") or ""
            hdr = f"- [{ttl}]({url})" if url else f"- {ttl}"
            st.markdown(f"{hdr}  _{src} · {tier}_")
            snippet = ev.get("snippet")
            if snippet:
                st.caption(snippet)

    official = data.get("official_sources") or []
    if official:
        with st.expander(f"Official sources ({len(official)})"):
            for s in official:
                st.markdown(
                    f"- [{s.get('title','?')}]({s.get('url','')})  "
                    f"_{s.get('source_name','')} · {s.get('tier','')}_"
                )


# ─── Topic / claim detail ────────────────────────────────────────────────────

def _render_topic(entry: Optional[dict[str, Any]]) -> None:
    if not entry:
        st.caption(
            "No topic detail yet. Ask *\"what topics are trending?\"* or "
            "*\"how is the emotional tone?\"* to load this tab."
        )
        return
    source = entry["source"]
    data = entry["data"]

    if source == "topic_overview":
        topics = data.get("topics") or []
        if not topics:
            st.info("No topics for this run.")
            return
        for t in topics:
            with st.container(border=True):
                label = t.get("label") or "(unnamed)"
                trending = "  :red[🔥 trending]" if t.get("is_trending") else ""
                st.markdown(f"**{label}**{trending}")
                cols = st.columns(4)
                cols[0].metric("posts", t.get("post_count", 0))
                cols[1].metric("velocity/h", f"{t.get('velocity', 0.0):.2f}")
                cols[2].metric("risk", f"{t.get('misinfo_risk', 0.0):.2f}")
                cols[3].metric("emotion", t.get("dominant_emotion") or "—")
        return

    if source == "emotion_analysis":
        st.markdown(
            f"**Dominant emotion:** `{data.get('dominant_emotion', '—')}`"
        )
        dist = data.get("overall_emotion_distribution") or {}
        if dist:
            st.dataframe(
                sorted(
                    [
                        {"emotion": k, "share": round(float(v), 3)}
                        for k, v in dist.items()
                    ],
                    key=lambda r: -r["share"],
                ),
                use_container_width=True,
                hide_index=True,
            )
        interp = data.get("interpretation")
        if interp:
            st.caption(interp)
        topics = data.get("topic_emotions") or []
        if topics:
            with st.expander(f"Per-topic breakdown ({len(topics)})"):
                st.dataframe(
                    [
                        {
                            "topic": t.get("label", ""),
                            "dominant": t.get("dominant_emotion", ""),
                        }
                        for t in topics
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
        return

    if source == "claim_status":
        st.markdown(f"**Claim:** _{data.get('claim_text', '')}_")
        verdict = data.get("verdict_label", "?")
        st.markdown(f"**Verdict:** {_VERDICT_BADGE.get(verdict, verdict)}")
        actionability = data.get("claim_actionability")
        if actionability:
            st.caption(f"Actionability: `{actionability}`")
        return

    if source == "explain_decision":
        dec = data.get("decision") or {}
        if dec:
            st.markdown(f"**Decision:** `{dec.get('decision', '?')}`")
            if dec.get("explanation"):
                st.write(dec["explanation"])
            if dec.get("recommended_next_step"):
                st.caption(f"Next: {dec['recommended_next_step']}")
            if dec.get("skip_reason"):
                st.caption(f"Skip reason: {dec['skip_reason']}")
        else:
            st.info("No intervention decision for this run.")
        cm = data.get("counter_message")
        if cm:
            with st.expander("Counter-message"):
                st.write(cm)
        history = data.get("history") or []
        if history:
            with st.expander(f"Prior deployments ({len(history)})"):
                st.dataframe(
                    [
                        {
                            "topic": r.get("topic_label", ""),
                            "deployed_at": r.get("deployed_at", ""),
                            "outcome": r.get("outcome", ""),
                        }
                        for r in history
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
        return

    st.json(data)


# ─── Graph ───────────────────────────────────────────────────────────────────

# Color palette indexed by node label / community
_LABEL_COLORS = {
    "Account": "#4f81bd",   # blue
    "Post":    "#9bbb59",   # green
    "Topic":   "#c0504d",   # red
    "Entity":  "#8064a2",   # purple
}
_COMMUNITY_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#bcbd22", "#17becf", "#7f7f7f",
]


def _render_graph(entry: Optional[dict[str, Any]]) -> None:
    if not entry:
        st.caption(
            "Ask *\"who is driving this topic?\"* / *\"trace the reply chain\"* "
            "/ *\"is this coordinated?\"* to populate this tab."
        )
        return
    source = entry.get("source", "")
    data = entry.get("data") or {}

    # ── v2 path: KGOutput ────────────────────────────────────────────────────
    if source == "kg" or "query_kind" in data:
        _render_kg_output(data)
        return

    # ── legacy path: v1 propagation_analysis dict ────────────────────────────
    cols = st.columns(3)
    cols[0].metric("Posts",     data.get("post_count", 0))
    cols[1].metric("Accounts",  data.get("unique_accounts", 0))
    cols[2].metric("Velocity /h", f"{data.get('velocity', 0.0):.2f}")
    if data:
        with st.expander("Raw data"):
            st.json(data)


def _render_kg_output(data: dict[str, Any]) -> None:
    """Render a KGOutput payload (v2 KG branch)."""
    kind = data.get("query_kind", "?")
    metrics = data.get("metrics") or {}
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []

    st.markdown(f"**Query:** `{kind}`")
    if metrics:
        # Show key metrics as columns when there are 1-4 numeric ones
        numeric = [(k, v) for k, v in metrics.items()
                   if isinstance(v, (int, float, bool))]
        if 1 <= len(numeric) <= 4:
            cols = st.columns(len(numeric))
            for i, (k, v) in enumerate(numeric):
                cols[i].metric(k.replace("_", " "),
                               f"{v:.3f}" if isinstance(v, float) else str(v))
        else:
            st.json(metrics)

    if not nodes:
        st.info("No graph nodes returned.")
        if data.get("target"):
            with st.expander("Query target"):
                st.json(data["target"])
        return

    _render_kg_graph(nodes, edges)
    _render_kg_node_table(nodes)


def _render_kg_graph(nodes: list[dict], edges: list[dict]) -> None:
    """Try the interactive agraph renderer; fall back to a legend-only view."""
    try:
        from streamlit_agraph import agraph, Node, Edge, Config  # type: ignore
    except ImportError:
        st.caption("(install `streamlit-agraph` for interactive view)")
        return

    # Pick a colour: explicit community_id wins, then label.
    def _color(n: dict) -> str:
        props = n.get("properties") or {}
        cid = props.get("community_id")
        if isinstance(cid, int):
            return _COMMUNITY_PALETTE[cid % len(_COMMUNITY_PALETTE)]
        return _LABEL_COLORS.get(n.get("label", ""), "#999999")

    def _label(n: dict) -> str:
        props = n.get("properties") or {}
        if n.get("label") == "Post":
            author = props.get("author") or props.get("author_id") or ""
            return author or n["id"][:12]
        if n.get("label") == "Account":
            return n["id"]  # already human-readable handle in our schema
        if n.get("label") == "Entity":
            return props.get("name") or n["id"]
        return n["id"]

    def _title(n: dict) -> str:
        props = n.get("properties") or {}
        bits = [f"id: {n['id']}", f"label: {n.get('label', '?')}"]
        for k, v in props.items():
            if v in (None, ""):
                continue
            bits.append(f"{k}: {str(v)[:120]}")
        return "\n".join(bits)

    a_nodes = []
    a_edges = []
    seen: set[str] = set()
    for n in nodes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        size = 18
        props = n.get("properties") or {}
        if "pagerank" in props:
            size = 12 + int(float(props["pagerank"]) * 250)
        elif "betweenness" in props:
            size = 12 + int(float(props["betweenness"]) * 60)
        elif "cascade_size" in props:
            size = 12 + int(props["cascade_size"]) * 4
        a_nodes.append(Node(
            id=n["id"], label=_label(n), title=_title(n),
            size=size, color=_color(n),
        ))
    for e in edges:
        if e.get("source_id") not in seen or e.get("target_id") not in seen:
            continue
        a_edges.append(Edge(
            source=e["source_id"], target=e["target_id"],
            label=e.get("rel_type", ""),
        ))

    cfg = Config(
        width="100%", height=420,
        directed=True, physics=True,
        nodeHighlightBehavior=True, highlightColor="#f7d774",
        collapsible=False,
    )
    agraph(nodes=a_nodes, edges=a_edges, config=cfg)


def _render_kg_node_table(nodes: list[dict]) -> None:
    """Tabular view below the graph: human-readable per-node properties."""
    rows = []
    for n in nodes:
        props = n.get("properties") or {}
        rows.append({
            "id": n["id"],
            "label": n.get("label", ""),
            "author": props.get("author") or props.get("author_id") or "",
            "name":   props.get("name") or "",
            "score": (round(props.get("pagerank"), 4)
                      if props.get("pagerank") is not None
                      else round(props.get("betweenness"), 4)
                           if props.get("betweenness") is not None
                           else props.get("cascade_size", "")),
            "community": props.get("community_id", ""),
            "preview": (props.get("text") or "")[:140],
        })
    if rows:
        with st.expander(f"Nodes ({len(rows)})", expanded=False):
            st.dataframe(rows, use_container_width=True, hide_index=True)


# ─── Metrics ─────────────────────────────────────────────────────────────────

_ARROWS = {"up": "↑", "down": "↓", "flat": "→", "unknown": "·"}


def _render_metrics(entry: Optional[dict[str, Any]]) -> None:
    if not entry:
        st.caption(
            "Ask *\"how does this run compare to the previous one?\"* to "
            "populate the metrics comparison."
        )
        return
    source = entry["source"]
    data = entry["data"]

    if source == "run_compare":
        st.markdown(
            f"**{data.get('target_run_id', '?')}** vs baseline "
            f"**{data.get('baseline_run_id', '?')}**"
        )
        changes = data.get("changes") or []
        if changes:
            st.dataframe(
                [
                    {
                        "field": ch.get("field", ""),
                        "baseline": ch.get("baseline"),
                        "target": ch.get("target"),
                        "delta": ch.get("delta"),
                        "dir": _ARROWS.get(ch.get("direction", "unknown"), "·"),
                    }
                    for ch in changes
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No comparable metrics found for these two runs.")
        narrative = data.get("narrative")
        if narrative:
            st.caption(narrative)
        return

    if source == "propagation_analysis":
        cols = st.columns(3)
        cols[0].metric("Posts", data.get("post_count", 0))
        cols[1].metric("Accounts", data.get("unique_accounts", 0))
        cols[2].metric("Velocity/h", f"{data.get('velocity', 0.0):.2f}")
        st.caption(
            "Propagation-derived metrics. Run a comparison to see deltas."
        )
        return

    st.json(data)


# ─── Visual ──────────────────────────────────────────────────────────────────

_VISUAL_BADGE = {
    "rendered": ":green[rendered]",
    "abstained": ":gray[abstained]",
    "no_decision": ":gray[no decision]",
    "render_failed": ":red[render failed]",
    "insufficient_data": ":orange[insufficient data]",
}


def _render_visual(entry: Optional[dict[str, Any]]) -> None:
    if not entry:
        st.caption(
            "Ask *\"summarise this in one image\"* or *\"make a clarification "
            "card\"* to populate this tab."
        )
        return
    source = entry["source"]
    data = entry["data"]

    if source == "visual_summary":
        status = data.get("status", "?")
        st.markdown(f"**Status:** {_VISUAL_BADGE.get(status, status)}")
        expl = data.get("explanation")
        if expl:
            st.caption(expl)
        path = data.get("image_path")
        if path:
            try:
                st.image(path, use_container_width=True)
            except Exception as exc:  # noqa: BLE001
                st.warning(f"Could not load image at {path}: {exc}")
        else:
            reason = data.get("reason")
            if reason:
                st.info(f"Reason: {reason}")
        dec = data.get("decision") or {}
        if dec:
            with st.expander("Intervention decision"):
                st.json(dec)
        return

    if source == "explain_decision":
        path = data.get("visual_card_path")
        if path:
            try:
                st.image(
                    path,
                    use_container_width=True,
                    caption="Pre-computed card",
                )
            except Exception as exc:  # noqa: BLE001
                st.warning(f"Could not load card at {path}: {exc}")
        else:
            st.info("No visual card attached to this decision.")
        return

    st.json(data)
