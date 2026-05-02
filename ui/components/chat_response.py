"""Structured widgets for chat outputs (v2 simplified).

The redesign-2026-05 v2 chat path returns:
    - branches_used: list[str]
    - branch_outputs: dict[str, list[dict]]   (per branch)
    - citations: list[Citation]

We render a compact summary plus a JSON dump per branch. Richer per-tab
rendering lives in `ui/components/analysis_tabs.py`.
"""
from __future__ import annotations

from typing import Any, Optional

import streamlit as st


def render_capability_output(
    capability_name: Optional[str],
    output: dict[str, Any],
) -> None:
    """Backwards-compat shim used by `ui/pages/0_Chat.py`.

    The v1 capability_output dict has been collapsed by the v2 orchestrator
    into a `{primary, aux_outputs, branches_used}` shape (see
    `agents/chat_orchestrator._legacy_capability_output`). We render the
    branches at the top level and let the right-side analysis tabs do the
    detailed display.
    """
    if not output:
        return
    if "error" in output:
        st.warning(f"Branch error: {output['error']}")
        return

    branches_used = output.get("branches_used") or []
    if branches_used:
        st.caption("Branches used: " + ", ".join(branches_used))

    primary = output.get("primary")
    aux = output.get("aux_outputs") or {}

    if primary:
        with st.expander("Primary branch output", expanded=False):
            st.json(primary)
    for name, payload in aux.items():
        with st.expander(f"Auxiliary output - {name}", expanded=False):
            st.json(payload)


def render_branch_outputs(
    branches_used: list[str],
    branch_outputs: dict[str, list[Any]],
) -> None:
    """Native v2 renderer (simple JSON view per branch)."""
    if not branches_used:
        return
    st.caption("Branches used: " + ", ".join(branches_used))
    for name in branches_used:
        outs = branch_outputs.get(name) or []
        if not outs:
            continue
        with st.expander(f"{name} ({len(outs)} result"
                            f"{'s' if len(outs) > 1 else ''})", expanded=False):
            for i, payload in enumerate(outs):
                st.markdown(f"**#{i + 1}**")
                st.json(payload)


def render_citations(citations: list[dict[str, Any]]) -> None:
    if not citations:
        return
    with st.expander(f"Citations ({len(citations)})", expanded=False):
        for c in citations:
            title = c.get("title") or c.get("source") or c.get("chunk_id", "?")
            url = c.get("url") or ""
            domain = c.get("domain") or ""
            tier = c.get("tier") or ""
            line = f"- [{title}]({url})" if url else f"- {title}"
            tail = " · ".join(filter(None, [domain, tier]))
            if tail:
                line += f"  _{tail}_"
            st.markdown(line)
