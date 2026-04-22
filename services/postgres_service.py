"""
Postgres service — structured operational data store.
Wraps psycopg2; all queries use parameterised statements (no SQL injection).
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import structlog

from config import POSTGRES_DSN
from models import IncidentReport, RunLog, StageStatus

log = structlog.get_logger(__name__)


class PostgresService:
    def __init__(self, dsn: str = POSTGRES_DSN) -> None:
        self._dsn = dsn
        self._conn: Optional[psycopg2.extensions.connection] = None

    def connect(self) -> None:
        self._conn = psycopg2.connect(self._dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        self._conn.autocommit = False
        log.info("postgres.connected", dsn=self._dsn)

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    @contextmanager
    def cursor(self):
        # Attempt up to 2 tries: first with the existing connection, then after
        # reconnecting (handles cases where the server restarted under us).
        for attempt in range(2):
            try:
                if self._conn is None or self._conn.closed:
                    self.connect()
                cur = self._conn.cursor()
                try:
                    yield cur
                    self._conn.commit()
                except Exception:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                    raise
                finally:
                    cur.close()
                return  # success — exit the retry loop
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                if attempt == 0:
                    # Connection dropped (e.g. server restarted); reconnect once
                    log.warning("postgres.reconnecting", error=str(exc)[:80])
                    self._conn = None
                else:
                    raise

    # ── Posts ──────────────────────────────────────────────────────────────────

    def upsert_post(self, post_id: str, account_id: str, text: str,
                    lang: str = "en", retweet_count: int = 0,
                    like_count: int = 0, reply_count: int = 0,
                    has_image: bool = False,
                    posted_at: Optional[datetime] = None) -> None:
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO posts (id, account_id, text, lang, retweet_count,
                                   like_count, reply_count, has_image, posted_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    retweet_count = EXCLUDED.retweet_count,
                    like_count    = EXCLUDED.like_count,
                    reply_count   = EXCLUDED.reply_count
            """, (post_id, account_id, text, lang, retweet_count,
                  like_count, reply_count, has_image, posted_at))

    def upsert_image(self, image_id: str, post_id: str, url: Optional[str],
                     local_path: Optional[str], ocr_text: Optional[str],
                     image_caption: Optional[str], image_type: Optional[str],
                     embedding_id: Optional[str]) -> None:
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO images (id, post_id, url, local_path, ocr_text,
                                    image_caption, image_type, embedding_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    ocr_text      = EXCLUDED.ocr_text,
                    image_caption = EXCLUDED.image_caption,
                    image_type    = EXCLUDED.image_type,
                    embedding_id  = EXCLUDED.embedding_id
            """, (image_id, post_id, url, local_path, ocr_text,
                  image_caption, image_type, embedding_id))

    def upsert_account(self, account_id: str, username: str,
                       display_name: Optional[str] = None,
                       verified: bool = False, followers: int = 0) -> None:
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO accounts (id, username, display_name, verified, followers)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    followers = EXCLUDED.followers
            """, (account_id, username, display_name, verified, followers))

    # ── Claims ─────────────────────────────────────────────────────────────────

    def upsert_claim(self, claim_id: str, normalized_text: str,
                     first_seen_post: Optional[str] = None,
                     propagation_count: int = 1,
                     risk_level: Optional[str] = None) -> None:
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO claims (id, normalized_text, first_seen_post,
                                    propagation_count, risk_level, updated_at)
                VALUES (%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (id) DO UPDATE SET
                    propagation_count = EXCLUDED.propagation_count,
                    risk_level        = EXCLUDED.risk_level,
                    updated_at        = NOW()
            """, (claim_id, normalized_text, first_seen_post,
                  propagation_count, risk_level))

    def link_post_claim(self, post_id: str, claim_id: str) -> None:
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO post_claims (post_id, claim_id)
                VALUES (%s,%s) ON CONFLICT (post_id, claim_id) DO NOTHING
            """, (post_id, claim_id))

    def increment_claim_propagation(self, claim_id: str) -> None:
        with self.cursor() as cur:
            cur.execute("""
                UPDATE claims SET propagation_count = propagation_count + 1,
                                   updated_at = NOW()
                WHERE id = %s
            """, (claim_id,))

    def get_claim(self, claim_id: str) -> Optional[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM claims WHERE id = %s", (claim_id,))
            return cur.fetchone()

    # ── Reports ────────────────────────────────────────────────────────────────

    def save_report(self, report: IncidentReport) -> None:
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO reports (id, intent_type, query_text, risk_level,
                    requires_review, propagation_json, counter_message,
                    visual_card_path, report_md)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    risk_level       = EXCLUDED.risk_level,
                    requires_review  = EXCLUDED.requires_review,
                    counter_message  = EXCLUDED.counter_message,
                    visual_card_path = EXCLUDED.visual_card_path,
                    report_md        = EXCLUDED.report_md
            """, (
                report.id,
                report.intent_type,
                report.query_text,
                report.risk_level,
                report.requires_human_review,
                json.dumps(report.propagation_summary.model_dump()
                           if report.propagation_summary else {}),
                report.counter_message,
                report.visual_card_path,
                report.report_md,
            ))
            for entry in report.run_logs:
                cur.execute("""
                    INSERT INTO run_logs (report_id, stage, status, detail)
                    VALUES (%s,%s,%s,%s)
                """, (report.id, entry.stage, entry.status.value, entry.detail))
