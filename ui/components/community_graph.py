"""Render the communities block from report_raw.json.

The raw pipeline output does not carry an edge list, so we use a simple
table + bar chart rather than an actual node/edge graph visual. Keeps the
UI honest about what is actually in the artifact.
"""
from __future__ import annotations

from typing import Any

import streamlit as st


def render(report_raw: dict[str, Any]) -> None:
    community = report_raw.get("community_analysis") or {}
    if not community:
        st.info("No community_analysis field in this run.")
        return

    skipped = community.get("skipped")
    if skipped:
        st.warning(
            f"Community detection was skipped: {community.get('skip_reason', 'unknown reason')}"
        )
        return

    cols = st.columns(3)
    cols[0].metric("Communities", community.get("community_count", "—"))
    cols[1].metric("Echo chambers", community.get("echo_chamber_count", "—"))
    cols[2].metric("Modularity Q", f"{community.get('modularity', 0.0):.3f}")

    communities = community.get("communities") or []
    if not communities:
        st.caption("No per-community rows.")
        return

    rows = [
        {
            "community_id": c.get("community_id"),
            "label": c.get("label"),
            "size": c.get("size"),
            "dominant_emotion": c.get("dominant_emotion"),
            "isolation_score": c.get("isolation_score"),
            "echo_chamber": c.get("is_echo_chamber"),
            "accounts": ", ".join((c.get("account_ids") or [])[:5]),
        }
        for c in communities
    ]
    st.dataframe(rows, use_container_width=True)
