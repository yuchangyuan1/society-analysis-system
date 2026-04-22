# Architecture

> **Status (2026-04-20):** outline scaffold. Fill in each `TODO` before delivery QA-6.
> Companion to `final_project_transformation_plan.md` (historical decision log, frozen) and `PROJECT_OVERVIEW.md` (Chinese top-level overview).

---

## 1. Data Flow (end-to-end)

TODO — insert diagram (pipeline overview) here.

```
Reddit/X ingestion  →  Claim extraction  →  Evidence retrieval (3 tiers)
                                                      │
                                        Actionability classification
                                                      │
                                          Intervention decision
                                             │            │
                                        Rebuttal     Evidence/Context    (or abstain)
                                             │            │
                                          Visual generation
                                                      │
                                          Metrics + Report
```

Tiers for evidence retrieval (`agents/knowledge.py`):
1. `internal_chroma` — project-local embedding store
2. `wikipedia` — `WikipediaService` REST API
3. `news` — NewsAPI (optional, gated by `NEWSAPI_KEY`)

## 2. Core Modules

| Layer | Path | Responsibility |
|---|---|---|
| Orchestration | `agents/planner.py` | Hard-orchestrated workflow, intent routing, run_dir lifecycle |
| Ingestion | `agents/ingestion.py` | Reddit / X / fixture input |
| Extraction | `agents/knowledge.py` | Claim extraction + 3-tier evidence pack |
| Analysis | `agents/analysis.py` | Propagation, velocity, account roles, topic clustering |
| Community | `agents/community.py` | Louvain community + modularity |
| Risk | `agents/risk.py` | Risk level + anomaly detection |
| Visual | `agents/visual.py` | Rebuttal Card + Evidence/Context Card generation |
| Report | `agents/report.py` | Template-rendered Markdown; LLM only wraps 2 sections |
| Counter-message | `agents/counter_message.py` | LLM-generated rebuttal text (existing gate) |
| Critic | `agents/critic.py` | Output sanity gate |

Services (pure, no LLM unless stated):
- `services/actionability_service.py` — rule-based actionability classifier
- `services/intervention_decision_service.py` — lookup-table decision
- `services/metrics_service.py` — `metrics.json` writer
- `services/manifest_service.py` — `run_manifest.json` writer, posts-hash
- `services/counter_effect_service.py` — cross-run follow-up tracking
- `services/wikipedia_service.py` — REST client + proper-noun counter
- `services/news_search_service.py` — NewsAPI client

## 3. Agent / Service Boundary

- **Agents** own an orchestration step and may call LLMs.
- **Services** are pure / deterministic helpers; they must not hide LLM calls from the planner.
- `actionability` and `intervention_decision` live in `services/` because they are rule-based and must be auditable without tracing through LLM prompts.
- `visual` is in `agents/` because it composes PIL drawing against an LLM-augmented layout — the boundary between deterministic template and generative wording is at the agent layer.

## 4. Why Actionability Is Inserted Before Output Routing

TODO — 1–2 paragraphs. Key points:
- Counter-messaging without actionability scored everything alike → wasted rebuttals on opinions.
- Classifying **before** the visual / counter-message branch lets `intervention_decision` pick the right output type.
- `primary_claim_id` is chosen after actionability so the "which claim to evaluate" question has evidence and actionability signals in scope.

## 5. Why Abstention Is a First-Class Result

TODO — 1–2 paragraphs. Key points:
- Empty output would be indistinguishable from a pipeline failure.
- `intervention_decision.decision == "abstain"` with `visual_type == None` is rendered in `report.md` with an explanation and a recommended next step (monitor / human_review / summarize).
- Makes research value visible: "we chose not to rebut, because …" is itself a result.

## 6. Run Artifacts

Per-run directory `data/runs/{run_id}/`:
- `run_manifest.json` — inputs, model, thresholds, git sha, posts hash
- `report.md` — template-rendered (Executive Summary + Flags are LLM-wrapped, ≤0.3 temperature)
- `report_raw.json` — full `IncidentReport` Pydantic dump
- `metrics.json` — per-run quantitative metrics
- `counter_visuals/*.png` — generated cards

See `PROJECT_OVERVIEW.md §二` for directory structure; see `final_project_transformation_plan.md §10` for the locked-in decision spec behind these choices.

## 7. Reproducibility Boundary

- **Fully reproducible**: `--claims-from` fixture path bypasses ingestion; metrics structural fields are stable across runs.
- **Not reproducible byte-level**: Claim-extraction LLM call distributes `first_seen_post` slightly differently across runs, which perturbs `account_role_counts`. Documented trade-off; not fixed in this revision.
