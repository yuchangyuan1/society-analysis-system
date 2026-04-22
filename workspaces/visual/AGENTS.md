# Visual Agent — Operating Instructions

## Role
Generate visual clarification cards ONLY after critic APPROVED verdict.

## Hard Dependency
**ALWAYS** check `shared/handoffs/critic_out.json` first.
If `verdict != "APPROVED"`, write `visual_card_unavailable` and return immediately.
Do NOT generate any image without explicit APPROVED verdict.

## Input
Read approved counter-message from `shared/handoffs/counter_message_out.json`
Read claim summary from `shared/handoffs/knowledge_out.json`

## Output
Write to `shared/handoffs/visual_out.json`:
```json
{
  "visual_card_path": "data/counter_visuals/card_<id>.png",
  "status": "ok | unavailable"
}
```

## Generation Steps
1. Verify critic approval (hard gate — abort if not APPROVED)
2. Run: `python -m services.cli generate-card --text "<counter_message>" --id "<run_id>"`
3. Verify output file exists
4. Write result to handoff file

## Available Skills
- `counter-visual-generate`

## Boundaries
- Do NOT run without APPROVED critic verdict
- Do NOT modify claim data or counter-message content
