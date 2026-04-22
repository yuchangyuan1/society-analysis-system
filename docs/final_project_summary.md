# Final Project Summary

> **Status (2026-04-20):** outline scaffold. Fill in each `TODO` before delivery QA-6.

---

## 1. Project Motivation

TODO — 2–3 sentences. Why build a multi-agent misinfo pipeline vs. a single-model classifier. Frame around: (a) evidence grounding, (b) actionability-aware intervention (not every claim should be rebutted), (c) reproducible run artifacts.

## 2. System Overview

TODO — one paragraph + one diagram (insert from `architecture.md §2 data flow`).

Ingestion → Claim extraction → Evidence retrieval (3 tiers) → **Actionability classification** → Intervention decision → Visual output → Metrics / report.

## 3. Why Multi-Agent / Multi-Stage

TODO — explain three points:
- Hard orchestration (planner) vs. soft reasoning (per-agent LLM calls inside bounded stages)
- Each stage has a verifiable output schema (Pydantic models under `models/`)
- Failure of one agent degrades a section, not the whole run

## 4. Key Design Choice — Actionability-Aware Intervention

TODO — core differentiation vs. common counter-misinfo pipelines.

- **Rule-based `ClaimActionability`** (`services/actionability_service.py`): 5-rule priority classifier, no LLM, deterministic.
- Three `non_actionable_reason` codes: `context_sparse`, `insufficient_evidence`, `non_factual_expression`.
- **Intervention decision lookup** (`services/intervention_decision_service.py`): `actionable` → `rebut` + Rebuttal Card; `non_actionable` with supporting evidence → `evidence_context` + Evidence/Context Card; otherwise → `abstain` (no PNG, still reported).

Abstention is a **first-class result**, not a failure.

## 5. Final Outputs

Each run produces `data/runs/{run_id}/`:
- `run_manifest.json`
- `report.md` (template-rendered; LLM only wraps Executive Summary + Flags and Next Steps)
- `report_raw.json`
- `metrics.json`
- `counter_visuals/*.png`

Plus two stable samples under `sample_runs/`:
- `run_fixed_claims_baseline/` — reproducible fixture run (actionability distribution coverage)
- `run_live_demo/` — live subreddit run (non-trivial social analysis)

## 6. Limitations

TODO — must enumerate honestly:
- **LLM non-determinism** in claim extraction (`first_seen_post` attribution drifts across runs, affecting `account_role_counts`). Structural metrics (`actionability_distribution`, `bridge_influence_ratio`, `role_risk_correlation`) are stable.
- **Fixture runs surface flat social metrics** — no subreddit graph, so `bridge_influence_ratio=0.0` and `role_risk_correlation=null`. Live runs are required to show signal.
- **English-only prompts** — extraction / stance classification not tested on other languages.
- **Counter-effect tracking is observational**, not causal — baseline vs. follow-up velocity is a correlation-level signal.
- **Actionability classifier is rule-based** — deliberately simple; it will misfire on idiomatic edge cases. Taxonomy not extended to keep the model auditable.
