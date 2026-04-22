---
name: emotion-analyze
description: |
  Per-post emotion classification and topic-level emotional tone aggregation.
  Use when: "analyze emotions", "what emotion dominates", "fear framing",
  "emotional tone", "anger vs hope", "classify post emotion"
version: 1.0.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: emotion-analyze

## Purpose
Classify the dominant emotional tone (fear | anger | hope | disgust | neutral) of each
ingested post using Claude.  Aggregate per-post emotions to the topic level to expose
the emotional fingerprint of each propagating narrative — a key predictor of misinformation
spread velocity (fear/anger frames travel 2-3× faster than neutral claims).

## Workspace
`knowledge`

## Trigger Conditions
Activate this skill when the planner or user asks to:
- Classify the emotional tone of ingested posts
- Compute topic-level emotion distribution
- Identify fear-framed or anger-framed narratives
- Produce an emotional heatmap for a campaign

## Step-by-Step Instructions

1. **Classify per-post emotions** (called from PlannerAgent Step 3b):
   ```bash
   # This is invoked automatically via KnowledgeAgent.classify_post_emotions(posts)
   # Called on ingested_posts after Step 2 (ingestion)
   ```
   Each post receives:
   - `post.emotion` — one of: `fear | anger | hope | disgust | neutral`
   - `post.emotion_score` — float 0.0–1.0 intensity

2. **Topic-level aggregation** (called from AnalysisAgent.analyze_topics()):
   - Counts per-emotion across all posts in the topic
   - Computes fractional distribution: `{"fear": 0.6, "anger": 0.3, "neutral": 0.1}`
   - Sets `TopicSummary.dominant_emotion` and `.emotion_distribution`

3. **Visual output**: Topic infographic cards automatically include a colour-coded
   emotion distribution bar when `ts.emotion_distribution` is populated.

## Input Format
```json
{
  "posts": [
    {"id": "string", "text": "string", "account_id": "string"}
  ]
}
```

## Output Format
```json
{
  "posts_classified": 42,
  "dominant_emotions": {
    "topic_id_1": {"dominant": "fear", "distribution": {"fear": 0.6, "anger": 0.3, "neutral": 0.1}}
  }
}
```

## Emotion Taxonomy
| Label   | Definition |
|---------|-----------|
| `fear`  | Danger, threat, alarm, worst-case framing ("they're coming for you") |
| `anger` | Outrage, blame, hostility, moral indignation |
| `hope`  | Optimism, solutions, positive future, agency |
| `disgust` | Revulsion, moral condemnation, loathing |
| `neutral` | Factual, informational, no dominant emotional tone |

## Error Handling
- API error → set `emotion = "neutral"`, `emotion_score = 0.0`; log warning
- Empty text → skip classification; leave `emotion = ""`
- Unrecognised LLM output → default to `"neutral"`
