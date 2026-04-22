---
name: meme-persuasion-analyze
description: |
  Decompose the persuasion tactics embedded in a social media claim or meme.
  Use when: "persuasion tactics", "why is this viral", "fear framing",
  "authority appeal", "virality score", "meme analysis", "persuasion features"
version: 1.0.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: meme-persuasion-analyze

## Purpose
Quantify the persuasion dimensions of a claim — emotional appeal, fear framing,
simplicity, authority reference, urgency markers, and identity triggers.
Outputs a composite virality_score and identifies the dominant tactic.

Corresponds to **Task 1.4** — Meme Persuasion Analysis.

## Workspace
`knowledge`

## Trigger Conditions
Activate when:
- TREND_ANALYSIS intent is active and claims are available
- User asks "why is this spreading so fast" or about persuasion mechanics
- Counter-messaging team needs to understand what makes a claim sticky

## Persuasion Dimensions
| Feature | Type | Description |
|---------|------|-------------|
| emotional_appeal | float 0–1 | Overall emotional charge |
| fear_framing | float 0–1 | Specifically fear-laden framing |
| simplicity_score | float 0–1 | 1.0 = very simple, easily shareable |
| authority_reference | bool | Cites expert / official source |
| urgency_markers | int | Count of "BREAKING", "CONFIRMED", etc. |
| identity_trigger | bool | Activates in-group / out-group identity |
| virality_score | float 0–1 | Weighted composite |
| top_persuasion_tactic | str | Dominant tactic label |

## Virality Score Formula
```
virality = fear_framing×0.30 + emotional_appeal×0.20 + simplicity×0.20
         + authority×0.15 + min(urgency,5)×0.02 + identity×0.13
```

## Output Format
```json
{
  "claim_id": "string",
  "emotional_appeal": 0.85,
  "fear_framing": 0.92,
  "simplicity_score": 0.70,
  "authority_reference": false,
  "urgency_markers": 2,
  "identity_trigger": true,
  "virality_score": 0.76,
  "top_persuasion_tactic": "fear_framing",
  "explanation": "The claim uses worst-case health framing combined with us-vs-them identity language."
}
```

## Error Handling
- API error → return zero-filled PersuasionFeatures, log warning
- Non-English claim → analyse available text, accuracy may be reduced
- Max 10 claims per call to limit API cost
