---
name: propagation-analyze
description: |
  Compute propagation metrics, stance distribution, and anomaly signals for social media posts.
  Use when: "propagation analysis", "trend report", "spreading patterns",
  "how fast is this spreading", "detect coordinated campaign", "velocity analysis",
  "anomaly detection in posts"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: propagation-analyze

## Purpose
Compute propagation metrics, stance distribution, and anomaly signals
for a set of posts about a topic or claim.

## Workspace
`analysis`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Analyze how fast a narrative is spreading
- Detect coordinated behavior or bot activity
- Generate a propagation trend report
- Assess stance distribution across posts on a topic

## Step-by-Step Instructions

1. Read ingested posts from `workspaces/shared/handoffs/ingestion_out.json`
2. Run propagation analysis via Python service:
   - Velocity = post_count / elapsed_hours
   - Stance distribution using keyword heuristics + LLM for ambiguous cases
   - Anomaly detection on sample of ≤ 30 posts (cost control)
3. Write results to:
   ```
   workspaces/shared/handoffs/analysis_out.json
   ```

## Input Format
```json
{
  "posts": "[Post] — list of ingested posts",
  "topic": "string? — topic label or claim text",
  "window_hours": "integer — time window for velocity (default 24)"
}
```

## Output Format
```json
{
  "propagation_summary": {
    "topic": "string",
    "post_count": "integer",
    "unique_accounts": "integer",
    "velocity": "float — posts per hour",
    "stance_distribution": {"supportive": 0, "against": 0, "neutral": 0},
    "anomaly_detected": false,
    "anomaly_description": "string or null"
  }
}
```

## Anomaly Signals to Detect
- Velocity spike: > 3x the baseline within a short window
- Account clustering: > 60% of posts from accounts created within the same 7-day window
- Identical wording: > 3 posts with character-level similarity > 0.95
- Stance imbalance: > 90% posts with identical stance (coordinated campaign indicator)

## Error Handling
| Error | Action |
|---|---|
| LLM analysis error | Log analysis_llm_error; return metric-only summary without anomaly description |
| Empty posts list | Return PropagationSummary with post_count=0 |
| Timestamp missing | Velocity = post_count; log timestamps_unavailable |
