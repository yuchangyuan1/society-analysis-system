---
name: misinfo-risk-review
description: |
  Evaluate misinformation likelihood for a claim given evidence and propagation context.
  Use when: "risk assessment", "is this misinformation", "flag for review",
  "how dangerous is this claim", "assess misinformation risk", "score this claim",
  "should this be escalated to human review"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: misinfo-risk-review

## Purpose
Evaluate the misinformation likelihood of a claim given its evidence pack
and propagation context. Assign a risk level and route high-risk outputs
to human review.

## Workspace
`risk`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Assess the risk level of a claim
- Determine if a claim should be flagged as misinformation
- Decide if human review is required
- Score propagation anomaly severity

## Step-by-Step Instructions

1. Read claim and evidence from `workspaces/shared/handoffs/knowledge_out.json`
2. Check `claim.has_sufficient_evidence()` — if false, return INSUFFICIENT_EVIDENCE immediately
3. Build context from claim text, evidence summary, and propagation metrics
4. Call Claude with extended thinking for nuanced assessment
5. If `propagation.anomaly_detected`, escalate risk to at least HIGH + requires_human_review
6. Write assessment to:
   ```
   workspaces/shared/handoffs/risk_out.json
   ```

## Input Format
```json
{
  "claim": "Claim object (with evidence populated)",
  "propagation_summary": "PropagationSummary? — optional"
}
```

## Output Format
```json
{
  "claim_id": "string",
  "risk_level": "LOW | MEDIUM | HIGH | CRITICAL | INSUFFICIENT_EVIDENCE",
  "misinfo_score": "float 0.0-1.0",
  "reasoning": "string (max 150 words)",
  "flags": ["string"],
  "requires_human_review": false,
  "propagation_anomaly": false
}
```

## Risk Level Mapping
| Condition | Risk Level |
|---|---|
| No evidence | INSUFFICIENT_EVIDENCE → block pipeline |
| Strong contradictions + high propagation | CRITICAL |
| Contradictions present + moderate propagation | HIGH |
| Mixed evidence + low propagation | MEDIUM |
| Supported by reliable sources | LOW |

## Block Conditions (routes to human review)
- `risk_level == INSUFFICIENT_EVIDENCE`
- `propagation_anomaly == true`
- `requires_human_review == true` (set by LLM)

## Error Handling
| Error | Action |
|---|---|
| LLM call fails | Return MEDIUM risk, requires_human_review=true, flag assessment_error |
| Invalid JSON from LLM | Log risk_json_parse_error; use defaults |
