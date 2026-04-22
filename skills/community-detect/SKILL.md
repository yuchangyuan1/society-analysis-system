---
name: community-detect
description: |
  Social network community detection, echo-chamber scoring, and cross-community
  coordination analysis.  Use when: "detect communities", "echo chamber",
  "community structure", "who coordinates", "social graph", "louvain"
version: 1.0.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: community-detect

## Purpose
Build an Account–Topic bipartite graph from the Kuzu knowledge graph, project it to an
Account–Account unipartite graph (shared topics = edges), and run Louvain community
detection to uncover social clusters.  Each community is scored for isolation (echo-chamber
likelihood), assigned a dominant emotion, and analysed for bridge accounts.
Cross-community coordination signals (different communities posting similar claims) are
extracted and persisted.

## Workspace
`community`

## Trigger Conditions
Activate this skill when the planner or user asks to:
- Detect social communities or clusters in the data
- Identify echo chambers in the propagation network
- Find bridge accounts connecting opposing communities
- Detect coordinated cross-community amplification
- Analyse network topology of the current dataset

## Step-by-Step Instructions

1. **Ensure dependencies are installed**:
   ```bash
   pip install networkx python-louvain
   ```

2. **Run community detection** (called from PlannerAgent Step 9c):
   ```bash
   # Automatically invoked via CommunityAgent.detect_communities(all_posts)
   # Triggered when IntentType.TREND_ANALYSIS is active and >= 10 accounts present
   ```

3. **Check output**: Results are attached to `IncidentReport.community_analysis`
   as a `CommunityAnalysis` object containing:
   - `community_count` — number of communities detected
   - `echo_chamber_count` — communities with isolation_score > 0.75
   - `modularity` — Louvain modularity Q (> 0.3 indicates good structure)
   - `communities[]` — per-community details with accounts, topics, emotion
   - `cross_community_signals[]` — coordination pairs across communities

## Minimum Data Requirements
- ≥ 10 accounts in the knowledge graph
- At least 1 Account–Topic edge (requires prior TREND_ANALYSIS run)
- If requirements not met → `CommunityAnalysis.skipped = True`

## Input Format
```json
{
  "posts": [{"id": "string", "account_id": "string", "emotion": "string"}]
}
```

## Output Format
```json
{
  "community_count": 4,
  "echo_chamber_count": 2,
  "modularity": 0.41,
  "communities": [
    {
      "community_id": "0",
      "label": "Community-0",
      "size": 48,
      "isolation_score": 0.91,
      "dominant_topics": ["topic_id_1", "topic_id_2"],
      "dominant_emotion": "fear",
      "is_echo_chamber": true,
      "bridge_accounts": ["u/crosspost_user"]
    }
  ],
  "cross_community_signals": [
    {
      "account_a": "u/account1", "community_a": "0",
      "account_b": "u/account2", "community_b": "2",
      "shared_claim_count": 4
    }
  ]
}
```

## Error Handling
| Failure | Handling |
|---------|---------|
| `networkx` not installed | Return `skipped=True`, log warning |
| `python-louvain` not installed | Return `skipped=True`, log warning |
| < 10 accounts | Return `skipped=True`, log info |
| Louvain exception | Log error, return `skipped=True` |
| Kuzu query timeout | Log error, skip persisting; return in-memory result |
