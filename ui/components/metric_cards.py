"""Top-of-page metric cards for a single run."""
from __future__ import annotations

from typing import Any

import streamlit as st


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render(metrics: dict[str, Any], manifest: dict[str, Any]) -> None:
    cols = st.columns(4)
    cols[0].metric(
        "Evidence coverage",
        _fmt(metrics.get("evidence_coverage")),
        help="Share of claims with ≥1 piece of evidence attached.",
    )
    cols[1].metric(
        "Community Q",
        _fmt(metrics.get("community_modularity_q"), digits=3),
        help="Louvain modularity. None → community detection was skipped.",
    )
    cols[2].metric(
        "Closed-loop rate",
        _fmt(metrics.get("counter_effect_closed_loop_rate")),
        help="Counter-effect records with a non-PENDING outcome.",
    )
    cols[3].metric(
        "Posts analysed",
        _fmt(manifest.get("post_count"), digits=0),
    )

    tier = metrics.get("evidence_tier_distribution") or {}
    if tier:
        with st.expander("Evidence tier distribution"):
            st.json(tier)
    roles = metrics.get("account_role_counts") or {}
    if roles:
        with st.expander("Account role counts"):
            st.json(roles)
