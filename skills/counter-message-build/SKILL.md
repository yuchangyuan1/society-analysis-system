---
name: counter-message-build
description: |
  Generate a concise, evidence-backed rebuttal for a high-risk misinformation claim.
  Use when: "counter message", "rebuttal", "debunk this", "write a correction",
  "generate clarification", "create fact-check response", "respond to misinformation"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: counter-message-build

## Purpose
Generate a concise, evidence-backed rebuttal text for a claim
that has been assessed as misinformation or high-risk.

## Workspace
`counter_message`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Generate a counter-message or rebuttal to a misinformation claim
- Write a fact-check correction
- Create a clarification for a debunked claim
- Revise a previously rejected counter-message (pass critic feedback as context)

## Prerequisites (must be satisfied before calling)
- `claim.has_sufficient_evidence() == true`
- `risk.risk_level != INSUFFICIENT_EVIDENCE`
- `risk.requires_human_review == false`

## Step-by-Step Instructions

1. Read claim and risk assessment from handoff files:
   - `workspaces/shared/handoffs/knowledge_out.json`
   - `workspaces/shared/handoffs/risk_out.json`
2. Build context from claim text, risk reasoning, and top evidence items
3. Generate counter-message via Claude (max 500 chars)
4. If revision_feedback is provided, incorporate it into the revised draft
5. Write output to:
   ```
   workspaces/shared/handoffs/counter_message_out.json
   ```

## Input Format
```json
{
  "claim": "Claim object (with evidence populated)",
  "risk": "RiskAssessment object",
  "revision_feedback": "string? — critic feedback from previous rejection",
  "max_chars": "integer — default 500"
}
```

## Output Format
```json
{
  "counter_message": "string — rebuttal text, max 500 chars",
  "char_count": "integer"
}
```

## Writing Rules
1. Factual and neutral tone (no preachy or aggressive language)
2. Cite specific evidence (article titles or source descriptions)
3. Target < 280 characters for social sharing; hard cap 500
4. End with: "Verify before sharing."
5. Never make claims beyond what the evidence supports
6. Acknowledge conflicting evidence if present

## Error Handling
| Error | Action |
|---|---|
| Insufficient evidence | Raise ValueError; blocked by planner before reaching this skill |
| LLM call fails | Re-raise; planner catches and routes to human review |
| Response over max_chars | Truncate at word boundary, append … |
