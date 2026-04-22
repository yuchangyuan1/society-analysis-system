---
name: campaign-report-build
description: |
  Compile all analysis outputs into a final structured IncidentReport with Markdown narrative.
  Use when: "compile report", "incident report", "final analysis", "summarize findings",
  "generate report", "build campaign report", "write analysis report"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: campaign-report-build

## Purpose
Compile all analysis outputs into a final structured IncidentReport
with a human-readable Markdown narrative. Persist to Postgres.

## Workspace
`report`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Compile a final analysis or incident report
- Summarize findings from all pipeline stages
- Generate a Markdown report of the misinformation analysis
- Persist the run results to the database

## Step-by-Step Instructions

1. Read all handoff files:
   - `workspaces/shared/handoffs/ingestion_out.json`
   - `workspaces/shared/handoffs/knowledge_out.json`
   - `workspaces/shared/handoffs/analysis_out.json`
   - `workspaces/shared/handoffs/risk_out.json`
   - `workspaces/shared/handoffs/counter_message_out.json`
   - `workspaces/shared/handoffs/visual_out.json`
   - `workspaces/shared/handoffs/critic_out.json`
2. Build context from all inputs
3. Generate Markdown report via Claude (max_tokens=1024)
4. If LLM fails, use fallback structured template
5. Persist report + run_logs to Postgres
6. Write final report to `workspaces/shared/handoffs/report_out.json`

## Input Format
```json
{
  "intent_type": "string — pipe-separated IntentType values",
  "query_text": "string?",
  "claims": "[Claim]",
  "risk": "RiskAssessment?",
  "propagation_summary": "PropagationSummary?",
  "counter_message": "string?",
  "visual_card_path": "string?",
  "run_log_items": "[RunLog]"
}
```

## Output Format
```json
{
  "id": "uuid",
  "intent_type": "string",
  "query_text": "string",
  "risk_level": "string",
  "requires_human_review": false,
  "propagation_summary": {},
  "counter_message": "string or null",
  "visual_card_path": "string or null",
  "report_md": "string — full Markdown report",
  "run_logs": [{"stage", "status", "detail", "logged_at"}],
  "created_at": "ISO-8601"
}
```

## Report Sections (Markdown)
1. **Executive Summary** — 2-3 sentences on findings and risk
2. **Claim Under Analysis** — normalized claim text and propagation count
3. **Evidence Assessment** — supporting vs. contradicting breakdown
4. **Propagation Analysis** — velocity, unique accounts, anomaly flag
5. **Risk Evaluation** — risk level, score, reasoning, flags
6. **Counter-Messaging Recommendation** — approved counter-message or human review notice
7. **Flags and Next Steps** — all raised flags and action items

## Surfaced Flags
- `image_text_unavailable` — OCR failed; analysis based on text only
- `graph_unavailable` — Kuzu query timed out; vector-only retrieval
- `visual_card_unavailable` — SD generation failed; text output only
- `human_review_required` — output sent to human review queue

## Error Handling
| Error | Action |
|---|---|
| LLM report generation fails | Use fallback template; log report_generation: DEGRADED |
| Postgres persist fails | Log report_persist: ERROR; return in-memory report |
