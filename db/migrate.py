"""
Database migration script — applies db/schema.sql to the configured Postgres instance.

Usage:
    python db/migrate.py

Requires POSTGRES_DSN to be set in .env or the environment.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def migrate() -> None:
    from config import POSTGRES_DSN

    log.info("migrate.connecting", dsn=POSTGRES_DSN.split("@")[-1])
    conn = psycopg2.connect(POSTGRES_DSN)
    conn.autocommit = True

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(schema_sql)
    conn.close()
    log.info("migrate.complete", schema=str(SCHEMA_PATH))
    print(f"\nSchema applied from {SCHEMA_PATH}")


if __name__ == "__main__":
    try:
        migrate()
    except Exception as exc:
        log.error("migrate.failed", error=str(exc))
        sys.exit(1)
