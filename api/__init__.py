"""Read-only research API over data/runs/{run_id}/ artifacts.

The API layer must NOT import from agents/* or services/*. It only reads
files written by the pipeline (run_manifest.json, report.md, report_raw.json,
metrics.json, counter_visuals/).
"""
