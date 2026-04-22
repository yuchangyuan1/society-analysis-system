"""Run Detail — manifest, metrics, rendered report, and visuals for a single run."""
from __future__ import annotations

import streamlit as st

from ui import api_client
from ui.components import community_graph, emotion_chart, metric_cards

st.set_page_config(page_title="Run Detail", layout="wide")
st.title("Run detail")

try:
    runs = api_client.list_runs()
except Exception as exc:  # noqa: BLE001
    st.error(f"Failed to load runs: {exc}")
    st.stop()

if not runs:
    st.info("No runs available yet.")
    st.stop()

run_ids = [r["run_id"] for r in runs]
default_index = 0
qs = st.query_params.get("run_id")
if qs and qs in run_ids:
    default_index = run_ids.index(qs)

run_id = st.selectbox("Select a run", run_ids, index=default_index)
st.query_params.update({"run_id": run_id})

try:
    summary = api_client.get_run(run_id)
    metrics = api_client.get_metrics(run_id)
    report_raw = api_client.get_report_raw(run_id)
except Exception as exc:  # noqa: BLE001
    st.error(f"Failed to load artifacts for {run_id}: {exc}")
    st.stop()

manifest = summary.get("manifest", {}) or {}

st.subheader("Overview")
metric_cards.render(metrics, manifest)

with st.expander("Run manifest", expanded=False):
    st.json(manifest)

tab_report, tab_community, tab_emotion, tab_counter, tab_raw = st.tabs(
    ["Report", "Community", "Emotion", "Counter-visuals", "Raw JSON"]
)

with tab_report:
    try:
        md = api_client.get_report_md(run_id)
        st.markdown(md)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to load report.md: {exc}")

with tab_community:
    community_graph.render(report_raw)

with tab_emotion:
    emotion_chart.render(report_raw)

with tab_counter:
    visuals = (summary.get("artifacts") or {}).get("counter_visuals") or []
    if not visuals:
        st.info("No counter-visual artifacts were saved for this run.")
    else:
        for name in visuals:
            st.caption(name)
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                st.image(api_client.visual_url(run_id, name))
            else:
                st.write(api_client.visual_url(run_id, name))

with tab_raw:
    st.json(report_raw)
