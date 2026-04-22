---
name: cascade-predict
description: |
  Heuristic 24-hour propagation cascade forecast for a trending topic.
  Use when: "predict spread", "how far will this go", "forecast reach",
  "cascade model", "24h prediction", "propagation forecast"
version: 1.0.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: cascade-predict

## Purpose
Given a trending `TopicSummary` and optional `CommunityAnalysis`, compute a heuristic
24-hour propagation forecast.  The model multiplies early-signal velocity by factors
derived from emotional tone, community isolation, and bridge account count.

Corresponds to **Task 1.10** — Information Cascade Prediction.

## Workspace
`analysis`

## Trigger Conditions
Activate this skill when:
- TREND_ANALYSIS intent is active and trending topics are detected
- User asks to predict 24h reach, peak window, or which communities will adopt
- Planner wants to prioritise counter-messaging by urgency

## Feature Set
| Feature | Source | Effect |
|---------|--------|--------|
| velocity | TopicSummary.velocity | Base growth (posts/hr × 24) |
| emotion_weight | dominant_emotion (fear=1.0, anger=0.8, ...) | Multiplier on spread rate |
| community_isolation | CommunityAnalysis.avg_isolation | Damper (high isolation → contained) |
| bridge_account_count | CommunityAgent output | Boost (bridges carry topic out) |
| misinfo_risk | TopicSummary.misinfo_risk | Influencer proxy boost |

## Output Format
```json
{
  "topic_id": "string",
  "topic_label": "string",
  "predicted_posts_24h": 420,
  "predicted_new_communities": 2,
  "peak_window_hours": "0-4h",
  "confidence": "MEDIUM",
  "reasoning": "velocity=8.3/hr, emotion=fear (weight=1.0), bridges=3, multiplier=2.8x"
}
```

## Confidence Levels
| Level | Condition |
|-------|----------|
| HIGH | ≥ 20 observed posts in topic |
| MEDIUM | 8–19 posts |
| LOW | < 8 posts |

## Error Handling
- Community data unavailable → proceeds with zero isolation / bridge values
- Single-post topic → returns LOW confidence forecast
- All outputs are heuristic estimates, not statistical predictions
