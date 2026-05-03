"""Presentation helpers for chat answers.

The chat UI should read like a research demo by default. Raw branch payloads
are still available, but only behind an explicit debug toggle.
"""
from __future__ import annotations

from typing import Any, Optional

import streamlit as st


_MODULES = {
    "evidence": {
        "name": "RAG",
        "description": "Official/evidence retrieval",
        "color": "#2563eb",
    },
    "kg": {
        "name": "Knowledge Graph",
        "description": "Network and propagation analysis",
        "color": "#7c3aed",
    },
    "nl2sql": {
        "name": "NL2SQL",
        "description": "Reddit structured data query",
        "color": "#047857",
    },
}


def render_source_list(citations: list[dict[str, Any]]) -> None:
    """Render evidence metadata as human-readable source references."""
    if not citations:
        return

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in citations:
        key = c.get("url") or c.get("chunk_id") or repr(c)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    with st.expander(f"Sources ({len(unique)})", expanded=False):
        for c in unique:
            title = c.get("title") or c.get("source") or "Untitled source"
            url = c.get("url") or ""
            source = c.get("source") or ""
            domain = c.get("domain") or ""
            tier = c.get("tier") or ""
            publish_date = c.get("publish_date") or ""

            label_bits = [bit for bit in (source, domain, tier) if bit]
            meta = " | ".join(label_bits)
            if publish_date:
                meta = f"{meta} | {str(publish_date)[:10]}" if meta else str(publish_date)[:10]

            if url:
                st.markdown(f"- [{title}]({url})")
            else:
                st.markdown(f"- {title}")
            if meta:
                st.caption(meta)


def render_method_summary(
    branches_used: list[str],
    branch_outputs: dict[str, list[Any]],
) -> None:
    """Show a compact, non-debug summary of the tools used."""
    if not branches_used:
        return

    labels = {
        "evidence": "Evidence retrieval",
        "nl2sql": "Reddit data query",
        "kg": "Knowledge graph",
    }
    rendered = [labels.get(b, b) for b in branches_used]
    st.caption("Methods used: " + ", ".join(rendered))

    metrics: list[str] = []
    evidence = _first(branch_outputs.get("evidence"))
    if evidence:
        chunks = ((evidence.get("bundle") or {}).get("chunks") or [])
        metrics.append(f"{len(chunks)} evidence chunks")

    for sql in branch_outputs.get("nl2sql") or []:
        rows = sql.get("rows") or []
        metrics.append(f"{len(rows)} Reddit rows")

    kg = _first(branch_outputs.get("kg"))
    if kg:
        nodes = kg.get("nodes") or []
        edges = kg.get("edges") or []
        if nodes or edges:
            metrics.append(f"{len(nodes)} graph nodes / {len(edges)} edges")

    if metrics:
        st.caption("Retrieved: " + ", ".join(metrics))


def render_route_modules(
    branches_used: list[str],
    branch_outputs: dict[str, list[Any]],
) -> None:
    """Render the active route modules as compact demo-facing cards."""
    ordered = [b for b in ("evidence", "kg", "nl2sql") if b in branches_used]
    ordered.extend([b for b in branches_used if b not in ordered])
    if not ordered:
        return

    st.markdown("#### Analysis Route")
    cols = st.columns(min(3, len(ordered)))
    for idx, branch in enumerate(ordered):
        meta = _MODULES.get(branch, {
            "name": branch,
            "description": "Analysis module",
            "color": "#4b5563",
        })
        summary = _branch_summary(branch, branch_outputs.get(branch) or [])
        with cols[idx % len(cols)]:
            st.markdown(
                f"""
                <div style="
                    border: 1px solid #e5e7eb;
                    border-left: 4px solid {meta['color']};
                    border-radius: 8px;
                    padding: 0.75rem 0.85rem;
                    background: #ffffff;
                    min-height: 6.4rem;
                ">
                  <div style="font-weight: 700; color: #111827;">
                    {meta['name']}
                  </div>
                  <div style="font-size: 0.86rem; color: #4b5563; margin-top: 0.2rem;">
                    {meta['description']}
                  </div>
                  <div style="font-size: 0.82rem; color: #6b7280; margin-top: 0.55rem;">
                    {summary}
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_knowledge_graph(branch_outputs: dict[str, list[Any]]) -> None:
    """Render a visual KG preview when the KG branch returned graph payloads."""
    kg_items = [
        item for item in (branch_outputs.get("kg") or [])
        if isinstance(item, dict)
    ]
    if not kg_items:
        return

    data = next(
        (item for item in kg_items if item.get("nodes") or item.get("edges")),
        kg_items[0],
    )
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    metrics = data.get("metrics") or {}
    title = "Knowledge Graph"
    if data.get("query_kind"):
        title += f" - {data['query_kind']}"

    with st.expander(title, expanded=bool(nodes or edges)):
        cols = st.columns(4)
        cols[0].metric("Nodes", len(nodes))
        cols[1].metric("Edges", len(edges))
        cols[2].metric("Graph nodes", metrics.get("node_count", len(nodes)))
        cols[3].metric("Graph edges", metrics.get("edge_count", len(edges)))

        if not nodes and not edges:
            reason = ((data.get("target") or {}).get("reason")
                      or "No graph result returned for this query.")
            st.info(reason)
            return

        _render_graph_canvas(nodes, edges)

        preview_cols = st.columns(2)
        with preview_cols[0]:
            st.markdown("**Top nodes**")
            st.dataframe(_node_rows(nodes[:20]), use_container_width=True,
                         hide_index=True)
        with preview_cols[1]:
            st.markdown("**Edges**")
            if edges:
                st.dataframe(_edge_rows(edges[:20]), use_container_width=True,
                             hide_index=True)
            else:
                st.caption("This result is a ranked-node graph with no explicit edge list in the response.")


def render_debug_outputs(
    branches_used: list[str],
    branch_outputs: dict[str, list[Any]],
    capability_name: Optional[str] = None,
    capability_output: Optional[dict[str, Any]] = None,
) -> None:
    """Render raw structured payloads for debugging."""
    if branches_used or branch_outputs:
        with st.expander("Technical details", expanded=False):
            st.caption("Branches used: " + ", ".join(branches_used or branch_outputs.keys()))
            for name in branches_used or list(branch_outputs.keys()):
                outs = branch_outputs.get(name) or []
                if not outs:
                    continue
                st.markdown(f"**{name}**")
                for payload in outs:
                    st.json(payload)
        return

    if capability_output:
        with st.expander("Technical details", expanded=False):
            if capability_name:
                st.caption(f"Capability: {capability_name}")
            st.json(capability_output)


def _first(items: Any) -> Optional[dict[str, Any]]:
    if isinstance(items, list) and items:
        first = items[0]
        return first if isinstance(first, dict) else None
    return None


def _branch_summary(branch: str, outputs: list[Any]) -> str:
    if branch == "evidence":
        chunks = 0
        for out in outputs:
            if isinstance(out, dict):
                chunks += len(((out.get("bundle") or {}).get("chunks") or []))
        return f"{chunks} evidence chunks" if chunks else "Ready for source verification"
    if branch == "nl2sql":
        rows = 0
        for out in outputs:
            if isinstance(out, dict):
                rows += len(out.get("rows") or [])
        return f"{rows} rows returned" if rows else "Structured SQL query"
    if branch == "kg":
        data = next((x for x in outputs if isinstance(x, dict)), {})
        nodes = len(data.get("nodes") or [])
        edges = len(data.get("edges") or [])
        if nodes or edges:
            return f"{nodes} nodes / {edges} edges"
        reason = ((data.get("target") or {}).get("reason") if data else None)
        return reason or "Graph query"
    return f"{len(outputs)} result payloads"


def _node_rows(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for node in nodes:
        props = node.get("properties") or {}
        rows.append({
            "id": node.get("id"),
            "type": node.get("label") or node.get("type"),
            "pagerank": props.get("pagerank"),
            "in_degree": props.get("in_degree"),
            "out_degree": props.get("out_degree"),
        })
    return rows


def _edge_rows(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for edge in edges:
        rows.append({
            "source": edge.get("source") or edge.get("from"),
            "target": edge.get("target") or edge.get("to"),
            "relation": edge.get("type") or edge.get("label") or edge.get("relation"),
        })
    return rows


def _render_graph_canvas(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    try:
        from streamlit_agraph import Config, Edge, Node, agraph
    except Exception:
        st.caption("Install streamlit-agraph to render the interactive graph canvas.")
        return

    graph_nodes = []
    for node in nodes[:80]:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        props = node.get("properties") or {}
        size = 16 + min(20, int((props.get("in_degree") or 0) * 2))
        graph_nodes.append(Node(
            id=node_id,
            label=node_id[:28],
            size=size,
            title=str(node.get("label") or ""),
        ))

    graph_edges = []
    node_ids = {n.id for n in graph_nodes}
    for edge in edges[:160]:
        source = str(edge.get("source") or edge.get("from") or "")
        target = str(edge.get("target") or edge.get("to") or "")
        if source in node_ids and target in node_ids:
            graph_edges.append(Edge(
                source=source,
                target=target,
                label=str(edge.get("type") or edge.get("relation") or ""),
            ))

    if not graph_nodes:
        st.caption("No graph nodes are available for visualization.")
        return

    config = Config(
        width="100%",
        height=420,
        directed=True,
        physics=True,
        hierarchical=False,
        nodeHighlightBehavior=True,
    )
    agraph(nodes=graph_nodes, edges=graph_edges, config=config)
