---
name: critic-review
description: |
  Quality gate: validate evidence coverage and detect overclaims in a counter-message draft.
  Use when: "review output", "quality check", "approve counter-message", "validate rebuttal",
  "check for overclaims", "critic review", "is this counter-message accurate"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Read
---

# Skill: critic-review

## Purpose
Validate evidence coverage and detect overclaims in a counter-message draft.
Approve or reject; route to human review after `CRITIC_MAX_RETRIES` (default 2) rejections.

## Workspace
`critic`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Review a counter-message draft for quality and accuracy
- Check if a rebuttal overclaims or misrepresents evidence
- Approve or reject a counter-message before publishing

## Step-by-Step Instructions

1. Check `attempt` — if attempt > CRITIC_MAX_RETRIES (2), return HUMAN_REVIEW immediately
2. Read counter-message from `workspaces/shared/handoffs/counter_message_out.json`
3. Read claim and evidence from `workspaces/shared/handoffs/knowledge_out.json`
4. Evaluate against approval criteria using Claude with extended thinking
5. Write verdict to:
   ```
   workspaces/shared/handoffs/critic_out.json
   ```

## Input Format
```json
{
  "counter_message": "string — draft to review",
  "claim": "Claim object (with evidence populated)",
  "attempt": "integer — current attempt number (1-indexed)"
}
```

## Output Format
```json
{
  "verdict": "APPROVED | REJECTED | HUMAN_REVIEW",
  "feedback": "string — explanation (max 100 words)",
  "rejection_log": ["string"]
}
```

## Approval Criteria (ALL must pass)
1. All claims are directly traceable to cited evidence
2. Conflicting evidence is acknowledged if present
3. No overconfident language ("definitely", "proven", "certainly") without strong evidence
4. Sources are accurately characterized
5. Tone is neutral and factual

## Rejection Criteria (ANY one is sufficient to fail)
- Overclaim: asserts certainty not supported by evidence
- Missing contradiction: ignores available contradicting evidence
- Beyond evidence: makes assertions not present in the evidence pack
- Inflammatory language

## Retry Logic
```
attempt 1: review → APPROVED or REJECTED (feedback → counter-message-build)
attempt 2: review → APPROVED or REJECTED (feedback → counter-message-build)
attempt 3+: return HUMAN_REVIEW (do NOT call LLM again)
```

## Error Handling
| Error | Action |
|---|---|
| LLM call fails | Return REJECTED with feedback = "Critic LLM failed" |
| Invalid JSON | Log critic_json_error; return REJECTED |
| attempt > CRITIC_MAX_RETRIES | Return HUMAN_REVIEW with rejection history |
