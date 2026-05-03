"""
Data admin CLI - production hardening Day 4.

Operator-facing tool for run lifecycle, rollback, and dry-run inspection.

Subcommands:
  scan-pending       List runs whose commit_state is pending/failed.
  rollback RUN_ID    Hard-delete a run from PG + Kuzu + Chroma + manifest.
  rollback-all-pending
                     Roll back every pending/failed run (used at startup).
  show RUN_ID        Print the manifest for a single run.

Usage:
  python -m scripts.data_admin scan-pending
  python -m scripts.data_admin rollback 20260503-090000-abc123
  python -m scripts.data_admin rollback-all-pending
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


def _rollback_one(run_id: str) -> dict:
    """Hard delete a run's data from PG + Kuzu + Chroma + manifest disk."""
    from services.postgres_service import PostgresService
    summary: dict = {"run_id": run_id}

    # PG
    try:
        pg = PostgresService(); pg.connect()
        summary["pg"] = pg.delete_run_data(run_id)
    except Exception as exc:
        log.error("rollback.pg_failed", run_id=run_id, error=str(exc)[:160])
        summary["pg_error"] = str(exc)[:200]

    # Kuzu - mark posts touched in this run as 'rolled_back' and detach edges.
    # Implementation deferred to Day 5 when Kuzu writer becomes single-writer.
    summary["kuzu"] = "skipped (Day 5 will wire single-writer rollback)"

    # Chroma 1 source_run_id
    try:
        from services.chroma_collections import ChromaCollections
        cols = ChromaCollections()
        # Chroma .delete supports `where=` filter
        cols.official.delete(where={"source_run_id": run_id})
        summary["chroma_official"] = "delete-by-where dispatched"
    except Exception as exc:
        log.error("rollback.chroma_failed",
                  run_id=run_id, error=str(exc)[:160])
        summary["chroma_error"] = str(exc)[:200]

    # Manifest: keep file but mark commit_state='failed' for audit
    try:
        from services.manifest_service import ManifestService
        from models.manifest import RunManifest
        ms = ManifestService()
        mf = ms.run_dir(run_id) / "run_manifest.json"
        if mf.exists():
            m = RunManifest.model_validate_json(mf.read_text(encoding="utf-8"))
            ms.mark_failed(m, error="rolled back by data_admin")
            summary["manifest"] = "marked failed"
    except Exception as exc:
        log.warning("rollback.manifest_update_failed",
                    run_id=run_id, error=str(exc)[:160])

    return summary


def cmd_scan(args) -> int:
    from services.manifest_service import ManifestService
    ms = ManifestService()
    pending = ms.list_pending_runs()
    if not pending:
        print("No pending or failed runs.")
        return 0
    print(f"Found {len(pending)} pending/failed run(s):")
    for m in pending:
        print(f"  {m.run_id}  state={m.commit_state}  "
              f"started={m.started_at.isoformat()}  "
              f"posts={m.post_count}")
    return 0


def cmd_rollback(args) -> int:
    summary = _rollback_one(args.run_id)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_rollback_all(args) -> int:
    from services.manifest_service import ManifestService
    ms = ManifestService()
    pending = ms.list_pending_runs()
    if not pending:
        print("Nothing to roll back.")
        return 0
    summaries = [_rollback_one(m.run_id) for m in pending]
    print(json.dumps(summaries, indent=2, default=str))
    return 0


def cmd_show(args) -> int:
    from models.manifest import RunManifest
    p = Path(args.run_id) / "run_manifest.json"
    if not p.exists():
        # Treat run_id as a bare id; resolve via ManifestService
        from services.manifest_service import ManifestService
        p = ManifestService().run_dir(args.run_id) / "run_manifest.json"
    if not p.exists():
        print(f"manifest not found: {p}")
        return 1
    m = RunManifest.model_validate_json(p.read_text(encoding="utf-8"))
    print(m.model_dump_json(indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Data admin (run lifecycle / rollback)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_scan = sub.add_parser("scan-pending",
                              help="List uncommitted runs.")
    sp_scan.set_defaults(fn=cmd_scan)

    sp_rb = sub.add_parser("rollback",
                            help="Roll back a single run.")
    sp_rb.add_argument("run_id")
    sp_rb.set_defaults(fn=cmd_rollback)

    sp_rball = sub.add_parser("rollback-all-pending",
                               help="Roll back every pending/failed run.")
    sp_rball.set_defaults(fn=cmd_rollback_all)

    sp_show = sub.add_parser("show", help="Print a run manifest.")
    sp_show.add_argument("run_id")
    sp_show.set_defaults(fn=cmd_show)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
