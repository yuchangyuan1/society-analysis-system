---
name: counter-effect-track
description: >
  Track the effectiveness of deployed counter-messages by recording a baseline
  velocity snapshot at deployment time and a follow-up measurement on the next
  run. Computes an effect_score ∈ [-1, +1] indicating whether the intervention
  slowed (positive) or accelerated (negative) misinformation propagation.
version: "1.0"
metadata:
  phase: 3
  tasks:
    - "3.2 — Counter-campaign deployment & effectiveness evaluation"
    - "3.7 — Competitive meme intervention monitoring"
  models:
    - models/counter_effect.py          # CounterEffectRecord, CounterEffectReport
  services:
    - services/counter_effect_service.py  # SQLite-backed persistence
  persistence: data/counter_effects.db   # SQLite, auto-created
allowed-tools:
  - Read
  - Write
  - Bash
---

## Overview

`counter-effect-track` is invoked automatically by the PlannerAgent whenever a
counter-message passes the critic gate.  It:

1. **Records a deployment baseline** — saves `baseline_velocity` and
   `baseline_post_count` from the current `PropagationSummary`.
2. **Detects pending follow-ups** — on subsequent runs for the same topic it
   calls `record_followup()` to capture the new velocity.
3. **Computes derived metrics**:
   - `velocity_delta = followup_velocity − baseline_velocity`  (negative = good)
   - `decay_rate = (baseline − followup) / baseline`
   - `effect_score = clamp(decay_rate, −1, +1)`
4. **Assigns an outcome label**: `EFFECTIVE` | `NEUTRAL` | `BACKFIRED` | `PENDING`

## effect_score interpretation

| Range          | Outcome    | Meaning                                  |
|----------------|------------|------------------------------------------|
| > 0.2          | EFFECTIVE  | Propagation measurably slowed            |
| −0.1 … 0.2     | NEUTRAL    | No significant change                    |
| < −0.1         | BACKFIRED  | Propagation accelerated after deployment |
| null           | PENDING    | Follow-up measurement not yet available  |

## Usage

```python
from services.counter_effect_service import CounterEffectService

svc = CounterEffectService()
# At counter-message deployment:
rec = svc.record_deployment(
    report_id="abc123",
    counter_message="Vaccines do not cause autism — see WHO study.",
    baseline_velocity=12.5,
    baseline_post_count=84,
    topic_id="topic_001",
    topic_label="Vaccine misinformation",
)
# On next run:
updated = svc.record_followup(rec.record_id, followup_velocity=7.2, followup_post_count=51)
print(updated.outcome)          # "EFFECTIVE"
print(updated.effect_score)     # 0.424

# Aggregate report:
report = svc.get_effect_report()
print(report.summary)
```
