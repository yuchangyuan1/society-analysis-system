"""
Idempotent migration: install the refresh_topic_post_count trigger on
posts_v2 and recompute every existing topics_v2.post_count once.

Without this trigger, topics_v2.post_count drifts whenever:
  - posts are rolled back via scripts.data_admin
  - a topic gets reassigned across runs
  - a topic row was written but its posts never landed

Safe to run repeatedly. Drops and recreates the trigger so re-runs pick up
schema changes; also re-derives post_count from posts_v2 each time so any
historical drift is corrected on first run.

Usage:
    python -m scripts.migrate_topic_post_count_trigger
"""
from __future__ import annotations

import sys

import structlog

import config

log = structlog.get_logger(__name__)


_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION refresh_topic_post_count() RETURNS trigger AS $$
DECLARE
    affected TEXT[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        affected := ARRAY[OLD.topic_id];
    ELSIF TG_OP = 'INSERT' THEN
        affected := ARRAY[NEW.topic_id];
    ELSE
        affected := ARRAY[NEW.topic_id, OLD.topic_id];
    END IF;

    UPDATE topics_v2 t
    SET post_count = (
            SELECT COUNT(*) FROM posts_v2 p WHERE p.topic_id = t.topic_id
        ),
        updated_at = NOW()
    WHERE t.topic_id = ANY(affected)
      AND t.topic_id IS NOT NULL;
    RETURN NULL;
END
$$ LANGUAGE plpgsql;
"""

_DROP_AND_CREATE = [
    "DROP TRIGGER IF EXISTS posts_v2_topic_count ON posts_v2",
    "DROP TRIGGER IF EXISTS posts_v2_topic_count_ins ON posts_v2",
    "DROP TRIGGER IF EXISTS posts_v2_topic_count_upd ON posts_v2",
    "DROP TRIGGER IF EXISTS posts_v2_topic_count_del ON posts_v2",
    """
    CREATE TRIGGER posts_v2_topic_count_ins
        AFTER INSERT ON posts_v2
        FOR EACH ROW EXECUTE FUNCTION refresh_topic_post_count()
    """,
    """
    CREATE TRIGGER posts_v2_topic_count_upd
        AFTER UPDATE OF topic_id ON posts_v2
        FOR EACH ROW
        WHEN (OLD.topic_id IS DISTINCT FROM NEW.topic_id)
        EXECUTE FUNCTION refresh_topic_post_count()
    """,
    """
    CREATE TRIGGER posts_v2_topic_count_del
        AFTER DELETE ON posts_v2
        FOR EACH ROW EXECUTE FUNCTION refresh_topic_post_count()
    """,
]


_RECOMPUTE_SQL = """
UPDATE topics_v2 t
SET post_count = sub.cnt,
    updated_at = NOW()
FROM (
    SELECT t2.topic_id,
           COALESCE(COUNT(p.post_id), 0) AS cnt
    FROM topics_v2 t2
    LEFT JOIN posts_v2 p ON p.topic_id = t2.topic_id
    GROUP BY t2.topic_id
) AS sub
WHERE t.topic_id = sub.topic_id
  AND t.post_count IS DISTINCT FROM sub.cnt
"""


def main() -> int:
    import psycopg2
    conn = psycopg2.connect(config.POSTGRES_DSN)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(_TRIGGER_SQL)
    log.info("migrate_trigger.function_ok")

    for stmt in _DROP_AND_CREATE:
        cur.execute(stmt)
    log.info("migrate_trigger.triggers_ok")

    cur.execute(_RECOMPUTE_SQL)
    fixed = cur.rowcount
    log.info("migrate_trigger.recompute_done", rows_fixed=fixed)

    conn.close()
    print(f"OK - trigger installed; {fixed} topics_v2 rows recomputed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
