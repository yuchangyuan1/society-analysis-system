"""Per-topic emotion mix visualisation."""
from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st


_EMOTIONS = ["anger", "fear", "joy", "sadness", "disgust", "surprise", "neutral"]


def render(report_raw: dict[str, Any]) -> None:
    topics = report_raw.get("topic_summaries") or []
    if not topics:
        st.info("No topic_summaries in this run.")
        return

    fig = go.Figure()
    labels = [t.get("label") or t.get("topic_id") or "?" for t in topics]
    for emo in _EMOTIONS:
        values = []
        for t in topics:
            mix = t.get("emotion_distribution") or {}
            values.append(float(mix.get(emo, 0.0)))
        if any(v > 0 for v in values):
            fig.add_trace(go.Bar(name=emo, x=labels, y=values))

    fig.update_layout(
        barmode="stack",
        height=360,
        margin=dict(l=20, r=20, t=20, b=80),
        yaxis=dict(title="share"),
        xaxis=dict(title="topic"),
    )
    st.plotly_chart(fig, use_container_width=True)
