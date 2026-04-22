---
name: immunity-strategy
description: >
  Graph-based propagation immunity strategy: select the minimal set of accounts
  for targeted inoculation (pre-bunking) that maximally reduces the spread of
  a misinformation claim across the network.  Uses betweenness centrality and
  PageRank to prioritise bridge and influencer nodes.
version: "1.0"
metadata:
  phase: 3
  tasks:
    - "1.8 — Propagation immunity strategy (graph-based vaccination targeting)"
  models:
    - models/immunity.py    # ImmunizationTarget, ImmunityStrategy
  agents:
    - agents/analysis.py    # AnalysisAgent.recommend_immunity_strategy()
  dependencies:
    - networkx>=3.0
    - python-louvain>=0.16  # via community detection (CommunityAgent must run first)
allowed-tools:
  - Read
  - Write
  - Bash
---

## Overview

`immunity-strategy` is invoked by the PlannerAgent for every `TREND_ANALYSIS`
run **after** community detection has completed.  It:

1. Builds an in-memory `networkx` graph from community membership data.
2. Computes `betweenness_centrality` and `pagerank` for every account.
3. Scores each account:

   ```
   priority = 0.5 × betweenness + 0.3 × pagerank
             + isolation_bonus (if bridge into echo chamber)
             + role_bonus (BRIDGE=0.8, AMPLIFIER=0.5)
   ```

4. Excludes `ORIGINATOR` accounts (targeting known spreaders is counterproductive).
5. Returns the top-N `ImmunizationTarget` objects with an estimated `immunity_coverage`.

## Coverage estimate

```
coverage = 1 − ∏(1 − pagerank_i)   for i in recommended_targets
```

Clamped to [0, 1].  Higher coverage means a larger fraction of the network
would be reached if all targets receive the inoculation message.

## Usage

```python
# Automatically invoked inside PlannerAgent.run() for TREND_ANALYSIS
# Manual invocation:
from agents.analysis import AnalysisAgent

strategy = analysis_agent.recommend_immunity_strategy(
    community_analysis=community_analysis,
    topic_id="topic_001",
    topic_label="Vaccine misinformation",
    max_targets=10,
)
print(strategy.summary)
for t in strategy.targets:
    print(t.account_id, t.priority_score, t.rationale)
```

## Graceful degradation

Returns `ImmunityStrategy(skipped=True)` when:
- `networkx` is not installed
- Community analysis was skipped or has no communities
- The graph contains zero accounts
