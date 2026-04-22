# Demo Script

> **Status (2026-04-20):** outline scaffold. Fill each `TODO` before answering with it live.
> Target length: ~12 minutes + Q&A. Keep print-friendly.

---

## 0. Pre-flight (before the room)

- [ ] `sample_runs/run_fixed_claims_baseline/` is up to date (last refreshed: TODO date)
- [ ] `sample_runs/run_live_demo/` is up to date **and** has non-null social analysis (`bridge_influence_ratio > 0` or `role_risk_correlation ≠ null`)
- [ ] Rebuttal Card + Evidence/Context Card PNGs open cleanly at 100% zoom
- [ ] `docs/architecture.md` pipeline diagram is rendered
- [ ] A printed copy of this script is on the desk (network-outage fallback)

## 1. Opening (≈1 min)

TODO — 3 sentences max. Suggested outline:
- Problem: misinfo response needs to stop treating every claim the same.
- Approach: evidence-grounded, actionability-aware intervention pipeline.
- Deliverable: reproducible run artifacts + two visual card types + social metrics.

## 2. Pipeline Overview (≈1 min)

Show the diagram from `architecture.md §1`. Name the 7 stages. Point out the **actionability classification** step as the pipeline's differentiator.

## 3. Input Modes (≈1 min)

Two inputs:
- `--claims-from tests/fixtures/claims_conspiracy_baseline.json` — fixture, for reproducibility.
- `--subreddit <name>` — live, for real-world evaluation.

> **Say out loud**: the fixture is for stability; the live run is for social analysis signal. Neither replaces the other.

## 4. Claim Extraction & Evidence Retrieval (≈1 min)

Open `sample_runs/run_fixed_claims_baseline/report.md` → `## Evidence Assessment`.

- Point at a claim with tier mix `internal_chroma + wikipedia`.
- Explain 3-tier retrieval without re-reading the whole section.

## 5. Actionability Decision (≈2 min)

This is the **spend-time** step.

- `## Claim Under Analysis` — every claim has `[actionability/reason]` tag.
- **Primary claim** appears first, marked `[PRIMARY]`.
- `## Intervention Decision` shows the decision routing.

Walk through one actionable case and one non-actionable case (fixture covers all 3 reasons).

## 6. Two Visual Outputs (≈2 min)

Open both cards side by side:
- **Rebuttal Card** — from an `actionable` decision.
- **Evidence / Context Card** — from a `non_actionable` + sufficient-supporting-evidence decision; two columns (Supported facts / Analyst note).

> **Say**: abstention (no card) is still a decision — it is rendered in the report with a recommended next step. This is a deliberate research stance, not a missing feature.

## 7. Social Analysis Snapshot (≈2 min, **live run only**)

Open `sample_runs/run_live_demo/report.md` → `## Social Analysis Snapshot`.

Current captured values (r/conspiracy, 3 days, 532 posts):
- `bridge_influence_ratio = 0.000`
- `role_risk_correlation = 0.000` (non-null)

> **Say**:
> - `bridge_influence_ratio = 0.0` does **not** mean the metric failed — it means no BRIDGE-role posters appeared in this window. BRIDGE requires an account to post across multiple discovered communities; the 1701 communities in this run are fine-grained enough that even heavy posters stayed within one. This is itself a finding: the r/conspiracy sample looks like siloed communities, not cross-pollinated ones.
> - `role_risk_correlation = 0.0` means both high-risk (misinfo_risk ≥ 0.6) and low-risk (< 0.3) topic buckets had posts, and ORIGINATOR share was equal across them. That is a **meaningful observational result** (non-null), not an empty metric. If ORIGINATORs were concentrated in high-risk topics we'd see a positive value; if in low-risk, negative.
> - Fixture runs set both to 0.0 / null trivially because there is no subreddit graph to mine. Live runs make these numbers informative, even when the answer is "no imbalance detected."

## 8. Why Non-actionable Is Not a Failure (≈1 min)

Tie back to §5. Three reasons: `context_sparse`, `insufficient_evidence`, `non_factual_expression`. For each, name the recommended next step from the intervention decision lookup.

---

## Q&A — Expected Follow-ups

### Q: Your actionability classifier is just 5 rules. Why not use an LLM?

TODO — suggested answer:
- Determinism + auditability. Rule firing order is written out in `services/actionability_service.py`.
- LLM classifiers drift across model versions; this runs identically regardless of which OpenAI model is configured.
- Trade-off acknowledged in `docs/final_project_summary.md §6 Limitations`.

### Q: `role_risk_correlation` — can you say that high-risk topics *cause* originator concentration?

TODO — suggested answer:
- No. It is a correlational metric on a snapshot. The plan explicitly disclaims causal interpretation (`final_project_transformation_plan.md §10.5`).
- Useful as a surveillance signal, not as policy evidence.

### Q: Fixture and live runs show different numbers — which one is right?

TODO — suggested answer:
- Both, for different axes. Fixture is for **structural reproducibility** (actionability distribution, intervention decision stability). Live is for **social analysis signal**. Neither subsumes the other.

### Q: What happens if the LLM call fails?

TODO — suggested answer:
- Report template rendering is deterministic; only Executive Summary + Flags and Next Steps are LLM-wrapped. Fact-verification diff logic in `agents/report.py` falls back to a deterministic narrative if LLM output drifts from structured state.

### Q: Counter-effect tracking closed-loop rate is …?

TODO — suggested answer:
- Cross-run, observational. Baseline velocity captured at deployment; follow-up velocity captured on the next run hitting the same topic or claim. `EFFECTIVE / NEUTRAL / BACKFIRED` labels are a thresholded delta, documented in `services/counter_effect_service.py`.

---

## Terminology — use these forms consistently

- **actionable** / **non-actionable** (not "rebut-able" / "un-rebut-able")
- **abstention** (not "skip", not "empty")
- **intervention decision** (not "routing" unless clarifying)
- **primary claim** (not "main claim" / "top claim")
- **Rebuttal Card** / **Evidence/Context Card** (both title-cased)
- **run artifact** (not "output file")
