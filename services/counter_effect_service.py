"""
Phase 3 — Counter-effect tracking service.

Corresponds to:
  Task 3.2  — Counter-campaign deployment & effectiveness evaluation
  Task 3.7  — Competitive meme intervention monitoring

Persistence: SQLite (Python built-in).  The DB file is created automatically
at data/counter_effects.db relative to the project working directory.

Workflow:
  1. When PrecomputePipeline deploys a counter-message, call record_deployment().
  2. On the next TREND_ANALYSIS run for the same topic, call record_followup().
  3. compute_effect_score() derives velocity_delta, decay_rate, effect_score, outcome.
  4. get_effect_report() aggregates all records into a CounterEffectReport.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

from config import COUNTER_EFFECTS_DB
from models.counter_effect import CounterEffectRecord, CounterEffectReport

log = structlog.get_logger(__name__)

_DB_PATH = Path(COUNTER_EFFECTS_DB)

# outcome thresholds
_EFFECTIVE_THRESHOLD = 0.2      # effect_score > 0.2
_BACKFIRED_THRESHOLD = -0.1     # effect_score < -0.1


class CounterEffectService:
    """SQLite-backed store for counter-message effectiveness records."""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # DB bootstrap
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS counter_effect_records (
                    record_id           TEXT PRIMARY KEY,
                    report_id           TEXT NOT NULL,
                    claim_id            TEXT,
                    topic_id            TEXT,
                    topic_label         TEXT,
                    counter_message     TEXT NOT NULL DEFAULT '',
                    deployed_at         TEXT NOT NULL,
                    baseline_velocity   REAL NOT NULL DEFAULT 0.0,
                    baseline_post_count INTEGER NOT NULL DEFAULT 0,
                    followup_velocity   REAL,
                    followup_post_count INTEGER,
                    followup_at         TEXT,
                    velocity_delta      REAL,
                    decay_rate          REAL,
                    effect_score        REAL,
                    outcome             TEXT DEFAULT 'PENDING'
                )
            """)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._db_path))
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def record_deployment(
        self,
        report_id: str,
        counter_message: str,
        baseline_velocity: float,
        baseline_post_count: int,
        claim_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        topic_label: Optional[str] = None,
    ) -> CounterEffectRecord:
        """Save a baseline snapshot immediately after counter-message deployment."""
        record_id = str(uuid.uuid4())
        now = datetime.utcnow()
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO counter_effect_records
                    (record_id, report_id, claim_id, topic_id, topic_label,
                     counter_message, deployed_at,
                     baseline_velocity, baseline_post_count, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,'PENDING')
                """,
                (
                    record_id, report_id, claim_id, topic_id, topic_label,
                    counter_message, now.isoformat(),
                    baseline_velocity, baseline_post_count,
                ),
            )
            con.commit()
        log.info("counter_effect.deployment_recorded",
                 record_id=record_id, topic_id=topic_id,
                 baseline_velocity=baseline_velocity)
        return CounterEffectRecord(
            record_id=record_id,
            report_id=report_id,
            claim_id=claim_id,
            topic_id=topic_id,
            topic_label=topic_label,
            counter_message=counter_message,
            deployed_at=now,
            baseline_velocity=baseline_velocity,
            baseline_post_count=baseline_post_count,
            outcome="PENDING",
        )

    def record_followup(
        self,
        record_id: str,
        followup_velocity: float,
        followup_post_count: int,
    ) -> CounterEffectRecord:
        """Record follow-up measurement and compute derived metrics."""
        now = datetime.utcnow()
        with self._connect() as con:
            con.execute(
                """
                UPDATE counter_effect_records
                SET followup_velocity   = ?,
                    followup_post_count = ?,
                    followup_at         = ?
                WHERE record_id = ?
                """,
                (followup_velocity, followup_post_count, now.isoformat(), record_id),
            )
            con.commit()
        record = self.compute_effect_score(record_id)
        log.info("counter_effect.followup_recorded",
                 record_id=record_id,
                 effect_score=record.effect_score,
                 outcome=record.outcome)
        return record

    def compute_effect_score(self, record_id: str) -> CounterEffectRecord:
        """
        Derive velocity_delta, decay_rate, effect_score and outcome for a record.

        effect_score formula:
          - decay_rate = (baseline - followup) / baseline  (clamped to [-1, 1])
          - effect_score = decay_rate  (positive = propagation slowed)
          - outcome thresholds: EFFECTIVE > 0.2 | BACKFIRED < -0.1 | else NEUTRAL
        """
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM counter_effect_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"No record found: {record_id}")

        rec = _row_to_record(row)
        if rec.followup_velocity is None:
            # Not yet measured — leave as PENDING
            return rec

        baseline = rec.baseline_velocity
        followup = rec.followup_velocity

        velocity_delta = followup - baseline  # negative = good (slowing)
        if baseline > 0:
            decay_rate = (baseline - followup) / baseline
        else:
            decay_rate = 0.0

        # Clamp to [-1, +1]
        effect_score = max(-1.0, min(1.0, decay_rate))

        if effect_score > _EFFECTIVE_THRESHOLD:
            outcome = "EFFECTIVE"
        elif effect_score < _BACKFIRED_THRESHOLD:
            outcome = "BACKFIRED"
        else:
            outcome = "NEUTRAL"

        with self._connect() as con:
            con.execute(
                """
                UPDATE counter_effect_records
                SET velocity_delta = ?,
                    decay_rate     = ?,
                    effect_score   = ?,
                    outcome        = ?
                WHERE record_id = ?
                """,
                (velocity_delta, decay_rate, effect_score, outcome, record_id),
            )
            con.commit()

        rec.velocity_delta = velocity_delta
        rec.decay_rate = decay_rate
        rec.effect_score = effect_score
        rec.outcome = outcome
        return rec

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_pending_followups(self) -> list[CounterEffectRecord]:
        """Return records that have been deployed but not yet followed up."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM counter_effect_records WHERE outcome = 'PENDING'"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_record(self, record_id: str) -> Optional[CounterEffectRecord]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM counter_effect_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_records_by_topic(self, topic_id: str) -> list[CounterEffectRecord]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM counter_effect_records WHERE topic_id = ? ORDER BY deployed_at DESC",
                (topic_id,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_pending_by_keys(
        self,
        topic_ids: Optional[list[str]] = None,
        topic_labels: Optional[list[str]] = None,
        claim_ids: Optional[list[str]] = None,
    ) -> list[CounterEffectRecord]:
        """
        P0-4 closed-loop helper: return PENDING records that match any of the
        given topic_ids / topic_labels / claim_ids. Used by PrecomputePipeline at the
        start of each run to follow up on prior deployments whose topic or
        claim reappears in the current data.
        """
        keys = {k for k in (topic_ids or []) if k} | {k for k in (claim_ids or []) if k}
        label_set = {l for l in (topic_labels or []) if l}
        if not keys and not label_set:
            return []
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM counter_effect_records WHERE outcome = 'PENDING'"
            ).fetchall()
        out: list[CounterEffectRecord] = []
        for r in rows:
            rec = _row_to_record(r)
            if rec.topic_id and rec.topic_id in keys:
                out.append(rec)
                continue
            if rec.claim_id and rec.claim_id in keys:
                out.append(rec)
                continue
            if rec.topic_label and rec.topic_label in label_set:
                out.append(rec)
        return out

    # ------------------------------------------------------------------
    # Aggregate report
    # ------------------------------------------------------------------

    def get_effect_report(self, top_n: int = 3) -> CounterEffectReport:
        """Aggregate all tracked records into a CounterEffectReport."""
        with self._connect() as con:
            all_rows = con.execute(
                "SELECT * FROM counter_effect_records ORDER BY deployed_at DESC"
            ).fetchall()

        records = [_row_to_record(r) for r in all_rows]
        total = len(records)
        pending = sum(1 for r in records if r.outcome == "PENDING")
        effective = sum(1 for r in records if r.outcome == "EFFECTIVE")
        neutral = sum(1 for r in records if r.outcome == "NEUTRAL")
        backfired = sum(1 for r in records if r.outcome == "BACKFIRED")

        scored = [r for r in records if r.effect_score is not None]
        avg_effect = (sum(r.effect_score for r in scored) / len(scored)) if scored else None  # type: ignore[arg-type]
        decay_scored = [r for r in records if r.decay_rate is not None]
        avg_decay = (sum(r.decay_rate for r in decay_scored) / len(decay_scored)) if decay_scored else None  # type: ignore[arg-type]

        sorted_scored = sorted(scored, key=lambda r: r.effect_score or 0.0, reverse=True)
        best = sorted_scored[:top_n]
        worst = sorted_scored[-top_n:] if len(sorted_scored) >= top_n else sorted_scored[::-1][:top_n]

        # Build summary sentence
        if total == 0:
            summary = "No counter-message deployments tracked yet."
        else:
            rate = round(effective / max(total - pending, 1) * 100)
            summary = (
                f"{total} deployment(s) tracked — "
                f"{effective} effective ({rate}%), "
                f"{neutral} neutral, "
                f"{backfired} backfired, "
                f"{pending} pending follow-up."
            )
            if avg_effect is not None:
                direction = "reduced" if avg_effect > 0 else "increased"
                summary += (
                    f" Average effect score: {avg_effect:+.2f} "
                    f"(propagation {direction} on average)."
                )

        return CounterEffectReport(
            total_tracked=total,
            pending_followup=pending,
            effective_count=effective,
            neutral_count=neutral,
            backfired_count=backfired,
            average_effect_score=avg_effect,
            average_decay_rate=avg_decay,
            best_performing=best,
            worst_performing=worst,
            summary=summary,
        )


# ------------------------------------------------------------------
# Internal helper
# ------------------------------------------------------------------

def _row_to_record(row: sqlite3.Row) -> CounterEffectRecord:
    def _dt(s: Optional[str]) -> Optional[datetime]:
        return datetime.fromisoformat(s) if s else None

    return CounterEffectRecord(
        record_id=row["record_id"],
        report_id=row["report_id"],
        claim_id=row["claim_id"],
        topic_id=row["topic_id"],
        topic_label=row["topic_label"],
        counter_message=row["counter_message"],
        deployed_at=_dt(row["deployed_at"]) or datetime.utcnow(),
        baseline_velocity=row["baseline_velocity"],
        baseline_post_count=row["baseline_post_count"],
        followup_velocity=row["followup_velocity"],
        followup_post_count=row["followup_post_count"],
        followup_at=_dt(row["followup_at"]),
        velocity_delta=row["velocity_delta"],
        decay_rate=row["decay_rate"],
        effect_score=row["effect_score"],
        outcome=row["outcome"],
    )
