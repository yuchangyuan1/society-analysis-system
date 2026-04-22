# Sample Runs

Stable, curated run artifacts for delivery / demo. Not auto-generated — refresh manually after code changes.

## Layout

```
sample_runs/
  run_fixed_claims_baseline/   # fixture-driven, reproducibility focus
  run_live_demo/               # live subreddit, social-analysis focus (TODO)
```

## `run_fixed_claims_baseline/`

Source: `python main.py --claims-from tests/fixtures/claims_conspiracy_baseline.json`

Purpose:
- Reproducible baseline with all 3 `non_actionable_reason` codes represented
- Stable `actionability_distribution`, `intervention_decision`
- Expected social metrics: `bridge_influence_ratio = 0.0`, `role_risk_correlation = null` — fixture has no subreddit graph (documented in `docs/final_project_summary.md §6 Limitations`)

Refresh:
```
python main.py --claims-from tests/fixtures/claims_conspiracy_baseline.json
# then copy the produced data/runs/{run_id}/ into sample_runs/run_fixed_claims_baseline/
```

## `run_live_demo/`

Source (2026-04-20): `python main.py --subreddit conspiracy --days 3`
Captured run: `20260420-041059-6afd7c` → 532 posts, 42 claims, 7 topics.

Observed metrics:
- `bridge_influence_ratio = 0.000` — no BRIDGE-role accounts in this window (most cross-community posting came from AMPLIFIERs, not BRIDGEs)
- `role_risk_correlation = 0.000` — **non-null** (passes QA-2). Both high-risk (misinfo_risk ≥ 0.6) and low-risk (< 0.3) topic buckets were populated; ORIGINATOR share was equal across them.
- `account_role_counts = {ORIGINATOR: 7, AMPLIFIER: 5, PASSIVE: 353}` — richer role mix than fixture (which has only PASSIVE)
- `modularity_q = 0.793` — strong community structure
- `intervention_decision.decision = "abstain"` with `reason = "insufficient_evidence"` — a **different intervention branch** than the fixture's `rebut`, giving the demo coverage across both decision paths
- 3 topic cards generated under `counter_visuals/`

Purpose:
- Demonstrate social analysis signal on a real subreddit
- Cover the `abstain` intervention branch
- Used as the backup demo run if live input fails at answer time

Refresh:
```
python main.py --subreddit conspiracy --days 3
# then copy the produced data/runs/{run_id}/ into sample_runs/run_live_demo/
```

**QA-2 (in `project_followup_execution_plan.md §四.5`)** — passes: `role_risk_correlation = 0.0 ≠ null`. `bridge_influence_ratio > 0` was not achieved in this window; BRIDGE detection depends on posters appearing across multiple discovered communities, which is a stricter condition than having ORIGINATOR + AMPLIFIER role mix. `demo_script.md §7` should explain this honestly rather than gloss over the 0.0.
