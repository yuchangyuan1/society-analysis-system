"""Run List — all pipeline runs, newest first."""
from __future__ import annotations

import streamlit as st

from ui import api_client

st.set_page_config(page_title="Run List", layout="wide")
st.title("Pipeline runs")

try:
    runs = api_client.list_runs()
except Exception as exc:  # noqa: BLE001
    st.error(f"Failed to load runs: {exc}")
    st.stop()

if not runs:
    st.info(
        "No runs yet. Run the pipeline once: `python main.py --subreddit conspiracy --days 1`"
    )
    st.stop()

st.caption(f"{len(runs)} run(s) under `data/runs/`. Click a run_id below to open it.")

rows = []
for r in runs:
    m = r.get("metrics", {}) or {}
    rows.append(
        {
            "run_id": r.get("run_id"),
            "started_at": r.get("started_at"),
            "finished_at": r.get("finished_at"),
            "query": r.get("query_text"),
            "subreddits": ", ".join(r.get("subreddits") or []),
            "posts": r.get("post_count"),
            "evidence_coverage": m.get("evidence_coverage"),
            "community_Q": m.get("community_modularity_q"),
            "closed_loop_rate": m.get("counter_effect_closed_loop_rate"),
            "model": r.get("openai_model"),
            "git_sha": (r.get("git_sha") or "")[:10],
        }
    )

st.dataframe(rows, use_container_width=True, hide_index=True)

st.markdown("---")
st.markdown(
    "To inspect a specific run, open the **Run Detail** page from the sidebar "
    "and paste the `run_id` from the table above."
)
