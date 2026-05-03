"""
Idempotent migration: add `first_seen_in_run` / `last_updated_in_run` columns
to existing posts_v2 / topics_v2 / entities_v2 tables. Existing rows are
backfilled with the literal 'legacy_pre_v6' so they stay queryable.

Safe to run on a fresh DB (no-op when columns already exist).

Usage:
    python -m scripts.migrate_run_lineage
"""
from __future__ import annotations

import sys

import structlog

import config

log = structlog.get_logger(__name__)

_TABLES_AND_COLS = [
    ("posts_v2",    "first_seen_in_run"),
    ("posts_v2",    "last_updated_in_run"),
    ("topics_v2",   "first_seen_in_run"),
    ("topics_v2",   "last_updated_in_run"),
    ("entities_v2", "first_seen_in_run"),
    ("entities_v2", "last_updated_in_run"),
]


def main() -> int:
    import psycopg2
    conn = psycopg2.connect(config.POSTGRES_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    added = 0
    for table, col in _TABLES_AND_COLS:
        try:
            cur.execute(
                f"ALTER TABLE {table} "
                f"ADD COLUMN IF NOT EXISTS {col} "
                f"TEXT NOT NULL DEFAULT 'legacy_pre_v6'"
            )
            added += 1
            log.info("migrate.col_ok", table=table, col=col)
        except Exception as exc:
            log.error("migrate.col_failed", table=table, col=col,
                      error=str(exc)[:160])
    conn.close()
    print(f"OK - {added} columns ensured (idempotent).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
