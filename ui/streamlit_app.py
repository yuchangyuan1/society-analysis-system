"""Streamlit entrypoint — research-only UI.

Run with:
    streamlit run ui/streamlit_app.py

The API must be running at RESEARCH_API_BASE (default http://127.0.0.1:8000).
"""
from __future__ import annotations

import streamlit as st

from ui import api_client

st.set_page_config(
    page_title="Society Analysis — Research",
    layout="wide",
)

st.title("Society Analysis — Research Console")
st.caption(
    "Start on the **Chat** page (sidebar) to ask questions conversationally, "
    "or use the Run List / Run Detail pages for the raw artefact view."
)

try:
    h = api_client.health()
    st.success(f"Connected to API. runs_root = `{h.get('runs_root')}`")
except Exception as exc:  # noqa: BLE001 — surface any network / parse error
    st.error(
        f"Could not reach research API at `{api_client.API_BASE}`: {exc}\n\n"
        "Start it with `uvicorn api.app:app --port 8000`."
    )

st.markdown(
    """
    ### Pages
    - **Run List** — all runs under `data/runs/`, newest first.
    - **Run Detail** — manifest, metrics, rendered report, and counter-message visuals for a single run.
    """
)
