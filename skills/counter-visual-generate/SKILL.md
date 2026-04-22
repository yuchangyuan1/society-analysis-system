---
name: counter-visual-generate
description: |
  Generate a 1200×675 px visual clarification card after critic approval.
  Use when: "generate visual card", "clarification image", "create fact-check card",
  "make a visual rebuttal", "generate counter image", "visual counter-message"
version: 1.1.0
metadata: {"openclaw": {"requires": {"env": ["ANTHROPIC_API_KEY"]}, "primaryEnv": "ANTHROPIC_API_KEY"}}
allowed-tools: Bash, Write
---

# Skill: counter-visual-generate

## Purpose
Generate a visual clarification card (1200×675 px, X post format)
combining a Stable Diffusion background with Pillow text overlay.

**Hard dependency**: must only be called after `critic-review` verdict = `APPROVED`.

## Workspace
`visual`

## Trigger Conditions
Activate this skill when the user or planner agent asks to:
- Generate a visual clarification or fact-check card
- Create a visual rebuttal image for social media
- Produce a counter-messaging image

**STOP immediately** if `workspaces/shared/handoffs/critic_out.json` shows verdict != "APPROVED".

## Step-by-Step Instructions

1. **Verify critic approval** (hard gate):
   ```bash
   # Read critic_out.json — if verdict != "APPROVED", stop and write visual_card_unavailable
   ```

2. **Generate the card**:
   ```bash
   python -m services.cli generate-card \
     --text "<counter_message>" \
     --id "<report_id>" \
     --bg-prompt "<SD background prompt>" \
     --claim-summary "<original_claim_summary>"
   ```

3. **Verify output file exists**, then write result to:
   ```
   workspaces/shared/handoffs/visual_out.json
   ```

## Input Format
```json
{
  "counter_message": "string — approved counter-message text",
  "claim": "Claim object",
  "report_id": "string — used for output filename"
}
```

## Output Format
```json
{
  "visual_card_path": "string — local file path to PNG, or null if failed",
  "status": "ok | unavailable"
}
```

## Generation Pipeline
1. Build SD prompt from claim context (no text in image prompt — Pillow handles text)
2. Generate 768×432 background via SD (upscale to 1200×675 via Pillow LANCZOS)
3. If SD fails, use programmatic gradient fallback background
4. Apply Pillow text overlay:
   - Header: "FACT CHECK" (yellow, 48pt)
   - Claim summary (grey, 22pt, truncated)
   - Counter-message (white, 28pt, word-wrapped)
   - Footer: "Source-backed analysis | Verify claims before sharing"
5. Save PNG to `data/counter_visuals/card_<report_id>_<timestamp>.png`

## Error Handling
| Error | Action |
|---|---|
| Critic not APPROVED | Stop immediately; write visual_card_unavailable |
| SD load fails | Degrade: use gradient fallback background |
| SD inference error | Degrade: use fallback; log sd_inference_failed |
| Pillow render error | Log pillow_error; return null |
| Disk write error | Log disk_write_error; return null |
