"""Structured widgets for chat capability outputs.

Each capability has its own renderer. Unknown capabilities fall back to
`st.json(...)`. The renderers take the already-serialized dict (what
comes out of `capability.run(...).model_dump(mode='json')`).
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st


def render_capability_output(
    capability_name: Optional[str],
    output: dict[str, Any],
) -> None:
    """Dispatch to per-capability renderer; fall back to raw JSON.

    Also surfaces planner-DAG context: the `workflow` name and any secondary
    step outputs under `aux_outputs` (e.g. a claim_verification flow that
    tacked on a propagation_analysis step). See `agents/planner.py`.
    """
    if "error" in output:
        st.warning(f"Capability error: {output['error']}")
        wf = output.get("workflow")
        if wf:
            st.caption(f"_workflow: `{wf}`_")
        return

    # 1. Workflow banner (only when the planner actually ran a template).
    wf = output.get("workflow")
    aux = output.get("aux_outputs") or {}
    if wf:
        steps = [capability_name] + [k for k in aux.keys()]
        steps_str = " → ".join(s for s in steps if s)
        st.caption(f"_workflow: `{wf}` · steps: {steps_str}_")

    # 2. Primary capability renderer.
    renderer = _RENDERERS.get(capability_name or "")
    if renderer is None:
        with st.expander("Raw capability output"):
            st.json(output)
    else:
        renderer(output)

    # 3. Aux step outputs (secondary DAG steps).
    if aux:
        with st.expander(f"Other DAG steps ({len(aux)})", expanded=False):
            for alias, sub_out in aux.items():
                st.markdown(f"**{alias}**")
                sub_renderer = _RENDERERS.get(alias)
                if sub_renderer is not None and isinstance(sub_out, dict):
                    sub_renderer(sub_out)
                else:
                    st.json(sub_out)


# ─── Per-capability renderers ────────────────────────────────────────────────

def _render_topic_overview(output: dict[str, Any]) -> None:
    topics = output.get("topics") or []
    if not topics:
        st.info("No topics for this run.")
        return
    rows = [
        {
            "label": t.get("label", ""),
            "posts": t.get("post_count", 0),
            "velocity": round(t.get("velocity", 0.0), 2),
            "risk": round(t.get("misinfo_risk", 0.0), 2),
            "emotion": t.get("dominant_emotion", ""),
            "trending": t.get("is_trending", False),
        }
        for t in topics
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_emotion_insight(output: dict[str, Any]) -> None:
    dominant = output.get("dominant_emotion") or "—"
    dist = output.get("overall_emotion_distribution") or {}
    st.markdown(f"**Dominant emotion:** `{dominant}`")
    if dist:
        rows = [
            {"emotion": k, "share": round(float(v), 3)}
            for k, v in sorted(dist.items(), key=lambda kv: -kv[1])
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    topics = output.get("topic_emotions") or []
    if topics:
        with st.expander(f"Per-topic breakdown ({len(topics)} topics)"):
            st.dataframe(
                [
                    {
                        "topic": t.get("label", ""),
                        "dominant": t.get("dominant_emotion", ""),
                        **{
                            k: round(float(v), 3)
                            for k, v in (t.get("emotion_distribution") or {}).items()
                        },
                    }
                    for t in topics
                ],
                use_container_width=True,
                hide_index=True,
            )


def _render_claim_status(output: dict[str, Any]) -> None:
    verdict = output.get("verdict_label", "?")
    badge = {
        "supported": ":green[supported]",
        "contradicted": ":red[contradicted]",
        "disputed": ":orange[disputed]",
        "insufficient": ":gray[insufficient evidence]",
        "non_factual": ":gray[non-factual]",
    }.get(verdict, verdict)
    st.markdown(f"**Verdict:** {badge}")
    st.markdown(f"**Claim:** _{output.get('claim_text','')}_")

    rationale = output.get("verdict_rationale")
    if rationale:
        st.info(rationale)

    counts = [
        {"stance": "supporting", "count": output.get("supporting_count", 0)},
        {"stance": "contradicting", "count": output.get("contradicting_count", 0)},
        {"stance": "uncertain", "count": output.get("uncertain_count", 0)},
    ]
    st.dataframe(counts, use_container_width=True, hide_index=True)

    tiers = output.get("evidence_tiers") or {}
    if tiers:
        st.caption(
            "Evidence tier histogram: "
            + ", ".join(f"`{k}`={v}" for k, v in sorted(tiers.items()))
        )

    for key, label in [
        ("top_supporting", "Top supporting evidence"),
        ("top_contradicting", "Top contradicting evidence"),
    ]:
        items = output.get(key) or []
        if not items:
            continue
        with st.expander(f"{label} ({len(items)})"):
            for ev in items:
                title = ev.get("article_title") or ev.get("article_id", "?")
                url = ev.get("article_url") or ""
                tier = ev.get("source_tier", "")
                src = ev.get("source_name") or ""
                hdr = f"- [{title}]({url})" if url else f"- {title}"
                st.markdown(f"{hdr}  _{src} · {tier}_")
                snip = ev.get("snippet")
                if snip:
                    st.caption(snip)

    retrieved = output.get("retrieved_chunks") or []
    if retrieved:
        with st.expander(f"On-demand Chroma retrieval ({len(retrieved)})"):
            for c in retrieved:
                title = c.get("title") or c.get("article_id", "?")
                url = c.get("url") or ""
                sim = c.get("similarity", 0.0)
                src = c.get("source_name") or ""
                hdr = f"- [{title}]({url})" if url else f"- {title}"
                st.markdown(f"{hdr}  _sim={sim:.3f} · {src}_")
                snip = c.get("snippet")
                if snip:
                    st.caption(snip)

    official = output.get("official_sources") or []
    if official:
        with st.expander(f"Official sources ({len(official)})"):
            for s in official:
                st.markdown(
                    f"- [{s.get('title','?')}]({s.get('url','')})  "
                    f"_{s.get('source_name','')} · {s.get('tier','')}_"
                )


def _render_propagation(output: dict[str, Any]) -> None:
    cols = st.columns(3)
    cols[0].metric("Posts", output.get("post_count", 0))
    cols[1].metric("Accounts", output.get("unique_accounts", 0))
    cols[2].metric("Velocity /h", f"{output.get('velocity', 0.0):.2f}")

    cols2 = st.columns(3)
    cols2[0].metric("Communities", output.get("community_count", 0))
    cols2[1].metric("Echo chambers", output.get("echo_chamber_count", 0))
    cols2[2].metric(
        "Bridge influence",
        f"{output.get('bridge_influence_ratio', 0.0):.2f}",
    )

    roles = output.get("account_role_summary") or {}
    if roles:
        st.markdown("**Account roles:**")
        st.dataframe(
            [{"role": k, "count": v} for k, v in roles.items()],
            use_container_width=True,
            hide_index=True,
        )

    communities = output.get("communities") or []
    if communities:
        with st.expander(f"Top communities ({len(communities)})"):
            st.dataframe(
                [
                    {
                        "id": c.get("community_id", ""),
                        "label": c.get("label", ""),
                        "size": c.get("size", 0),
                        "isolation": round(c.get("isolation_score", 0.0), 2),
                        "emotion": c.get("dominant_emotion", ""),
                        "echo_chamber": c.get("is_echo_chamber", False),
                    }
                    for c in communities
                ],
                use_container_width=True,
                hide_index=True,
            )

    if output.get("anomaly_detected"):
        st.warning(output.get("anomaly_description") or "Anomaly detected.")


def _render_visual_summary(output: dict[str, Any]) -> None:
    status = output.get("status", "?")
    expl = output.get("explanation", "")
    badge = {
        "rendered": ":green[rendered]",
        "abstained": ":gray[abstained]",
        "no_decision": ":gray[no decision]",
        "render_failed": ":red[render failed]",
        "insufficient_data": ":orange[insufficient data]",
    }.get(status, status)
    st.markdown(f"**Visual summary status:** {badge}")
    if expl:
        st.caption(expl)

    path = output.get("image_path")
    if path:
        try:
            st.image(path, use_container_width=True)
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Could not load image at {path}: {exc}")
    else:
        reason = output.get("reason")
        if reason:
            st.info(f"Reason: {reason}")

    dec = output.get("decision") or {}
    if dec:
        with st.expander("Intervention decision"):
            st.json(dec)


def _render_run_compare(output: dict[str, Any]) -> None:
    st.markdown(
        f"Comparing **{output.get('target_run_id','?')}** "
        f"vs **{output.get('baseline_run_id','?')}**"
    )
    changes = output.get("changes") or []
    if not changes:
        st.info("No comparable metrics found for these two runs.")
        return
    rows = []
    for ch in changes:
        arrow = {"up": "↑", "down": "↓", "flat": "→", "unknown": "·"}[
            ch.get("direction", "unknown")
        ]
        rows.append({
            "field": ch.get("field", ""),
            "baseline": ch.get("baseline"),
            "target": ch.get("target"),
            "delta": ch.get("delta"),
            "direction": arrow,
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_explain_decision(output: dict[str, Any]) -> None:
    dec = output.get("decision") or {}
    if not dec:
        st.info("No intervention decision available for this run.")
        return
    st.markdown(f"**Decision:** `{dec.get('decision','?')}`")
    if dec.get("explanation"):
        st.write(dec["explanation"])
    if dec.get("recommended_next_step"):
        st.caption(f"Recommended next step: {dec['recommended_next_step']}")

    cm = output.get("counter_message")
    if cm:
        with st.expander("Counter-message"):
            st.write(cm)

    path = output.get("visual_card_path")
    if path:
        try:
            st.image(path, caption="Pre-computed card", use_container_width=True)
        except Exception:  # noqa: BLE001
            pass

    history = output.get("history") or []
    if history:
        with st.expander(f"Prior deployments ({len(history)})"):
            st.dataframe(
                [
                    {
                        "record_id": r.get("record_id", ""),
                        "topic": r.get("topic_label", ""),
                        "deployed_at": r.get("deployed_at", ""),
                        "baseline_v": r.get("baseline_velocity", 0.0),
                        "followup_v": r.get("followup_velocity"),
                        "outcome": r.get("outcome", ""),
                    }
                    for r in history
                ],
                use_container_width=True,
                hide_index=True,
            )


_RENDERERS = {
    "topic_overview": _render_topic_overview,
    "emotion_analysis": _render_emotion_insight,
    "claim_status": _render_claim_status,
    "propagation_analysis": _render_propagation,
    "visual_summary": _render_visual_summary,
    "run_compare": _render_run_compare,
    "explain_decision": _render_explain_decision,
}
