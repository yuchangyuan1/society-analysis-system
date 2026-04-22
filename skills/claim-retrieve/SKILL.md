---
name: claim-retrieve
description: |
  Two-stage claim deduplication and evidence pack assembly from social media text.
  Use when: "extract claims", "check if claim exists", "find evidence for claim",
  "deduplicate claims", "build evidence pack", "retrieve fact-checks",
  "normalize post into verifiable assertions"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Read, Write
---

# Skill: claim-retrieve

## Purpose
Two-stage claim deduplication and evidence pack assembly.
Normalizes raw text into verifiable claims, resolves identity via
embedding similarity + LLM judge, and retrieves supporting / contradicting evidence.

## Workspace
`knowledge`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Extract factual claims from social media text
- Check whether a claim already exists in the knowledge base
- Build an evidence pack for fact-checking
- Run the deduplication pipeline on new posts

## Step-by-Step Instructions

1. **Extract claims** from the input text:
   ```bash
   python -m services.cli claim-extract --text "<input_text>" [--post-id "<post_id>"]
   ```

2. **Check deduplication** for each candidate claim (optional — handled internally):
   ```bash
   python -m services.cli claim-dedup --claim "<candidate_claim>"
   ```
   Returns: `{"result": "SAME"|"RELATED"|"DIFFERENT", "matched_id": "...", "similarity": 0.95}`

3. **Build evidence pack** for the resolved claim:
   ```bash
   python -m services.cli evidence-pack --claim-id "<claim_id>"
   ```

4. Write resolved claims to the handoff file:
   ```
   workspaces/shared/handoffs/knowledge_out.json
   ```

## Input Format
```json
{
  "text": "string — raw post or merged text",
  "post_id": "string? — originating post ID"
}
```

## Output Format
```json
{
  "claims": [
    {
      "id": "string",
      "normalized_text": "string",
      "propagation_count": "integer",
      "supporting_evidence": [],
      "contradicting_evidence": [],
      "uncertain_evidence": []
    }
  ]
}
```

## Deduplication Protocol
### Stage 1 — Embedding similarity (fast filter)
- Score ≥ 0.92 → proceed to Stage 2
- Score < 0.85 → insert as new claim
- Score in [0.85, 0.92) → proceed to Stage 2

### Stage 2 — LLM judgment (precision check)
- `SAME` → merge nodes, increment propagation_count
- `RELATED` → insert new, add related_to edge
- `DIFFERENT` → insert as new claim

## Error Handling
| Error | Action |
|---|---|
| LLM dedup error | Default to DIFFERENT; log dedup_llm_error |
| Chroma query error | Log graph_unavailable; treat as new claim |
| Evidence retrieval error | Return claim with empty evidence |
