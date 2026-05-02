"""Reflection panel - redesign-2026-05 Phase 5.3.

Three tabs over the experience stores:
    - Chroma 2: NL2SQL schema + success + error docs
    - Chroma 3: Planner module cards + workflow exemplars + composition errors
    - Audit log: PG `reflection_log` rows (Critic verdicts)

Read-only browse + manual delete (calls the FastAPI delete endpoint). The
view is intentionally simple: this is an operator tool, not a feature
surface. No charts; just tables + JSON expanders.
"""
from __future__ import annotations

import json
from typing import Any

import requests
import streamlit as st

from ui import api_client


st.set_page_config(page_title="Reflection", layout="wide")
st.title("Reflection inspector")
st.caption(
    "Operator view of Chroma 2 / Chroma 3 experience records and the "
    "Critic audit log."
)

tab_c2, tab_c3, tab_log = st.tabs([
    "Chroma 2 (NL2SQL)", "Chroma 3 (Planner)", "Audit log",
])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fetch(path: str, params: dict | None = None) -> dict[str, Any]:
    try:
        resp = requests.get(
            f"{api_client.API_BASE}{path}", params=params, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        st.error(f"GET {path} failed: {exc}")
        return {}


def _delete(path: str) -> bool:
    try:
        resp = requests.delete(f"{api_client.API_BASE}{path}", timeout=15)
        resp.raise_for_status()
        return True
    except Exception as exc:
        st.error(f"DELETE {path} failed: {exc}")
        return False


def _records_table(
    records: list[dict[str, Any]], collection: str,
) -> None:
    if not records:
        st.info("No records.")
        return
    rows: list[dict[str, Any]] = []
    for r in records:
        meta = r.get("metadata") or {}
        rows.append({
            "id": r.get("id", ""),
            "kind": meta.get("kind", ""),
            "confidence": meta.get("confidence"),
            "hit_count": meta.get("hit_count"),
            "last_used_at": meta.get("last_used_at"),
            "preview": (r.get("document") or "")[:140],
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.markdown("---")
    selected = st.text_input(
        f"Delete record id (from {collection})",
        key=f"del_{collection}",
    )
    col1, col2 = st.columns([1, 6])
    with col1:
        if st.button("Delete", key=f"btn_{collection}", type="primary"):
            if not selected.strip():
                st.warning("Enter a record id.")
            else:
                ok = _delete(f"/reflection/{collection}/{selected.strip()}")
                if ok:
                    st.success(f"Deleted {selected}")
                    st.rerun()
    with col2:
        st.caption("Manual purge. Use sparingly; the auto-curator runs every"
                   " day via `scripts/decay_chroma_experience.py`.")

    with st.expander("Inspect a record (full JSON)"):
        rid = st.text_input("Record id", key=f"inspect_{collection}")
        if rid:
            match = next((r for r in records if r["id"] == rid), None)
            if match:
                st.json(match)
            else:
                st.warning("Not in current page.")


# ── Tabs ─────────────────────────────────────────────────────────────────────

with tab_c2:
    kind = st.selectbox("Kind filter", ["", "schema", "success", "error"],
                         key="c2_kind")
    limit = st.slider("Limit", 50, 1000, 200, 50, key="c2_limit")
    payload = _fetch(
        "/reflection/chroma2",
        params={"kind": kind, "limit": limit} if kind
                else {"limit": limit},
    )
    _records_table(payload.get("records", []), "chroma2")


with tab_c3:
    kind = st.selectbox(
        "Kind filter",
        ["", "module_card", "workflow_success", "workflow_error",
          "composition_error"],
        key="c3_kind",
    )
    limit = st.slider("Limit", 50, 1000, 200, 50, key="c3_limit")
    payload = _fetch(
        "/reflection/chroma3",
        params={"kind": kind, "limit": limit} if kind
                else {"limit": limit},
    )
    _records_table(payload.get("records", []), "chroma3")


with tab_log:
    error_kind = st.text_input("error_kind filter (optional)",
                                  key="log_kind")
    limit = st.slider("Limit", 50, 500, 100, 50, key="log_limit")
    payload = _fetch(
        "/reflection/log",
        params={"error_kind": error_kind, "limit": limit} if error_kind
                else {"limit": limit},
    )
    rows = payload.get("rows", [])
    if not rows:
        st.info("No audit rows.")
    else:
        # Project a short table; row JSON is in expander
        short = [
            {
                "occurred_at": r.get("occurred_at"),
                "session_id": r.get("session_id"),
                "error_kind": r.get("error_kind"),
                "failed_branch": r.get("failed_branch"),
                "user_message": (r.get("user_message") or "")[:80],
            }
            for r in rows
        ]
        st.dataframe(short, use_container_width=True, hide_index=True)
        with st.expander(f"Full rows ({len(rows)})"):
            st.json(rows)
