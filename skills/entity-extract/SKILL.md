---
name: entity-extract
description: |
  Extract named entities from claims and build a co-occurrence graph.
  Use when: "what entities are mentioned", "named entity", "who appears",
  "entity co-occurrence", "relationship network", "who is involved"
version: 1.0.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: entity-extract

## Purpose
Extract named entities (PERSON, ORG, PLACE, EVENT) from the top claims,
build a co-occurrence graph in Kuzu (Claim→Entity Mentions edges and
Entity↔Entity CoOccursWith edges), and surface the most prominent actors
and relationships in the misinformation campaign.

Corresponds to **Task 1.3 supplement** — Entity Relationship Network.

## Workspace
`knowledge`

## Trigger Conditions
Activate when:
- TREND_ANALYSIS intent is active and claims are available
- User asks "who is mentioned", "entity network", "who's involved"
- Analysis team wants to identify recurring actors across claims

## Entity Types
| Type | Examples |
|------|---------|
| PERSON | "Anthony Fauci", "Elon Musk", "Bill Gates" |
| ORG | "FDA", "WHO", "Pfizer", "CDC" |
| PLACE | "United States", "Wuhan", "Brussels" |
| EVENT | "COVID-19 pandemic", "2024 election", "WEF Summit" |

## Graph Schema (Kuzu)
```
Entity {id, name, entity_type, mention_count}
Claim  ──Mentions──────▶ Entity
Entity ──CoOccursWith──▶ Entity
```

## Output Format
```json
{
  "entities": [
    {"entity_id": "uuid", "name": "WHO", "entity_type": "ORG", "mention_count": 12}
  ],
  "co_occurrences": [
    {
      "entity_a_name": "Bill Gates", "entity_b_name": "WHO",
      "co_occurrence_count": 7,
      "shared_claim_ids": ["claim-1", "claim-2"]
    }
  ]
}
```

## Error Handling
- API error per claim → skip that claim, continue
- No entities found → return empty lists (not an error)
- Max 30 claims processed per call to limit API cost
- Co-occurrence threshold: ≥ 2 shared claims to be reported
