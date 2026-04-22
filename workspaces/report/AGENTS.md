# Report Agent — Operating Instructions

## Role
Compile all analysis outputs into a final structured IncidentReport
with a human-readable Markdown narrative. Persist to Postgres.

## Input
Read from all handoff files in `shared/handoffs/`:
- `ingestion_out.json` — post counts and source
- `knowledge_out.json` — claims and evidence
- `analysis_out.json` — propagation summary
- `risk_out.json` — risk assessment
- `counter_message_out.json` — approved counter-message (if any)
- `critic_out.json` — critic verdict
- `visual_out.json` — visual card path (if any)

## Output
Write to `shared/handoffs/report_out.json` and also return the IncidentReport object.

## Report Sections (Markdown)
1. **Executive Summary** — 2-3 sentences on findings and risk
2. **Claim Under Analysis** — normalized text and propagation count
3. **Evidence Assessment** — supporting vs. contradicting breakdown
4. **Propagation Analysis** — velocity, unique accounts, anomaly flag
5. **Risk Evaluation** — risk level, score, reasoning, flags
6. **Counter-Messaging Recommendation** — approved message or human review notice
7. **Flags and Next Steps** — all raised flags and action items

## Rules
- If LLM report generation fails, use fallback structured template
- Always persist to Postgres via `PostgresService.save_report()`
- If Postgres is unavailable, log the error and return the in-memory report

## Available Skills
- `campaign-report-build`
