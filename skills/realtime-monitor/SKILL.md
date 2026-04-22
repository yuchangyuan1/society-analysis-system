---
name: realtime-monitor
description: >
  Polling-based real-time continuous monitoring: re-runs the full PlannerAgent
  pipeline at configurable intervals and emits structured alerts when velocity,
  misinfo risk, or cascade thresholds are crossed.  Supports --watch --interval N
  mode in main.py.
version: "1.0"
metadata:
  phase: 3
  tasks:
    - "2.1 — Real-time streaming data analysis"
    - "3.3 — Continuous watch mode"
  services:
    - services/monitor_service.py   # MonitorService, MonitorConfig, MonitorAlert
  dependencies: []    # no new dependencies; relies on existing pipeline
allowed-tools:
  - Read
  - Write
  - Bash
---

## Overview

`realtime-monitor` provides a polling loop around the existing multi-agent
pipeline.  Alert conditions are configurable:

| Alert type       | Trigger                                      | Default threshold |
|------------------|----------------------------------------------|-------------------|
| `HIGH_VELOCITY`  | topic velocity ≥ velocity_threshold          | 5.0 posts/hr      |
| `HIGH_RISK`      | topic misinfo_risk ≥ risk_threshold          | 0.70              |
| `CASCADE_WARNING`| predicted 24h posts ≥ cascade_threshold      | 200 posts         |
| `NEW_TOPIC`      | topic label not seen in any prior cycle      | always            |

Alerts are emitted via:
1. Console output (always)
2. `structlog` warning records (always)
3. Optional `on_alert` callback (Slack / email / PagerDuty hook)

## Usage

### From main.py (--watch flag)

```bash
python main.py --watch --interval 300 "vaccine misinformation"
```

### Programmatic

```python
from services.monitor_service import MonitorService, MonitorConfig

config = MonitorConfig(
    velocity_threshold=5.0,
    risk_threshold=0.70,
    cascade_threshold=200,
    max_cycles=10,        # None = run forever
)
monitor = MonitorService(planner, config=config)
monitor.start(query="5G vaccines", interval_seconds=300)
```

### Single cycle (no loop)

```python
result = monitor.run_once(query="5G vaccines")
print(result.alerts)
```

## Output format

```
[14:23:01] Cycle #1  status=OK  topics=4  trending=2  alerts=3
  🔴 [HIGH_VELOCITY] Vaccine claims: velocity=8.3 posts/hr (threshold=5.0)
  🟠 [CASCADE_WARNING] Vaccine claims: predicted_posts_24h=312 (threshold=200)
  🟢 [NEW_TOPIC] Climate denial: New trending topic detected
```
