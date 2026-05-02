"""
Reflection store - redesign-2026-05 Phase 3.6 (auto-removal portion).

Routes Critic verdicts to the right experience store and drops poison
records via Ablation. Full Reflection UI / scoring + decay arrives in
Phase 5; this Phase 3 cut handles the parts that *must* run in production
once NL2SQL and the Planner go live, otherwise Chroma 2 / Chroma 3 will
collect bad lessons and degrade quality.

Routing (PROJECT_REDESIGN_V2.md 7b-(3) Layer 2):
    sql_empty_result    -> Chroma 2 error (NL2SQL)
    missing_branch      -> Chroma 3 workflow_error (Planner)
    wrong_branch_combo  -> Chroma 3 workflow_error (Planner)
    citation_missing    -> Chroma 3 composition_error (Writer)
    numeric_mismatch    -> Chroma 3 composition_error (Writer)
    off_topic           -> Chroma 3 workflow_error or composition_error
                           depending on failed_branch.

Anti-thrash: same `record_id` deleted+rewritten more than 2 times in 24h
gets quarantined for 24 hours.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import structlog

from models.reflection import CriticVerdict, ReflectionRecord
from services.embeddings_service import EmbeddingsService
from services.nl2sql_memory import NL2SQLMemory
from services.planner_memory import PlannerMemory

log = structlog.get_logger(__name__)


_THRASH_WINDOW_SECONDS = 24 * 3600
_THRASH_LIMIT = 2


@dataclass
class _ThrashTracker:
    """Sliding-window counter per record_id."""

    history: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    def hit(self, record_id: str) -> int:
        now = time.time()
        q = self.history[record_id]
        while q and now - q[0] > _THRASH_WINDOW_SECONDS:
            q.popleft()
        q.append(now)
        return len(q)

    def is_quarantined(self, record_id: str) -> bool:
        return self.hit(record_id) > _THRASH_LIMIT


@dataclass
class ReflectionStore:
    """Routes a CriticVerdict into Chroma 2 / Chroma 3 + audit trail.

    Optional dependencies:
      - `pg`: when provided, also writes a row to `reflection_log`.
      - `ablation_runner`: a callable `(verdict, record_ids) -> bool` that
        re-runs the failing step without the suspect records and returns
        True iff the failure disappeared. Phase 3 ships a no-op default;
        Phase 5 wires the real one.
    """

    nl2sql_memory: Optional[NL2SQLMemory] = None
    planner_memory: Optional[PlannerMemory] = None
    embeddings: Optional[EmbeddingsService] = None
    pg: Optional[Any] = None  # services.postgres_service.PostgresService
    ablation_runner: Optional[Callable[[CriticVerdict, list[str]], bool]] = None
    _thrash: _ThrashTracker = field(default_factory=_ThrashTracker)

    def __post_init__(self) -> None:
        self.nl2sql_memory = self.nl2sql_memory or NL2SQLMemory()
        self.planner_memory = self.planner_memory or PlannerMemory()
        self.embeddings = self.embeddings or EmbeddingsService()
        self.ablation_runner = self.ablation_runner or (lambda v, ids: False)
        # Phase 5 fix: lazy-init Postgres for the audit trail. Failures
        # degrade silently (Reflection still curates Chroma 2/3 even if PG
        # is unreachable).
        if self.pg is None:
            try:
                from services.postgres_service import PostgresService
                self.pg = PostgresService()
                self.pg.connect()
            except Exception as exc:
                log.warning("reflection.pg_unavailable", error=str(exc)[:160])
                self.pg = None

    # ── Public ───────────────────────────────────────────────────────────────

    def record(
        self,
        verdict: CriticVerdict,
        *,
        user_message: str,
        session_id: Optional[str] = None,
        branches_used: Optional[list[str]] = None,
        payload: Optional[dict] = None,
    ) -> ReflectionRecord:
        record = ReflectionRecord(
            session_id=session_id,
            user_message=user_message,
            error_kind=verdict.error_kind,
            failed_branch=verdict.failed_branch,
            causal_record_ids=list(verdict.causal_record_ids),
            payload=payload or {},
        )
        # 1. Audit trail
        self._write_pg_audit(record)

        if verdict.passed:
            return record

        # 2. Ablation - which causal record(s) actually caused the failure?
        guilty_ids = self._find_guilty_records(verdict)
        for rid in guilty_ids:
            if not self._thrash.is_quarantined(rid):
                self._delete_record(rid)
            else:
                log.warning("reflection.quarantined", record_id=rid)

        # 3. Write a fresh negative lesson where it belongs
        self._route_lesson(verdict, user_message, branches_used or [])
        return record

    # ── Internals ────────────────────────────────────────────────────────────

    def _write_pg_audit(self, record: ReflectionRecord) -> None:
        if self.pg is None:
            return
        try:
            with self.pg.cursor() as cur:
                cur.execute("""
                    INSERT INTO reflection_log
                        (occurred_at, session_id, user_message, error_kind,
                         failed_branch, causal_record_ids, payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                """, (
                    record.occurred_at, record.session_id,
                    record.user_message, record.error_kind,
                    record.failed_branch, record.causal_record_ids,
                    _json_dump(record.payload),
                ))
        except Exception as exc:
            log.error("reflection.audit_write_failed", error=str(exc)[:120])

    def _find_guilty_records(self, verdict: CriticVerdict) -> list[str]:
        if not verdict.causal_record_ids:
            return []
        # Only ablate the first 3 (PROJECT_REDESIGN_V2.md cost cap)
        guilty: list[str] = []
        for rid in verdict.causal_record_ids[:3]:
            try:
                if self.ablation_runner(verdict, [rid]):
                    guilty.append(rid)
            except Exception as exc:
                log.warning("reflection.ablation_error", rid=rid,
                            error=str(exc)[:120])
        return guilty

    def _delete_record(self, record_id: str) -> None:
        # Determine which memory owns this id (NL2SQL ids start with
        # `schema::` / `success::` / `error::`; planner ids start with
        # `module_card::` / `workflow_*` / `composition_error::`).
        if record_id.startswith(("schema::", "success::", "error::")):
            self.nl2sql_memory.delete_records([record_id])
        else:
            self.planner_memory.delete_records([record_id])

    def _route_lesson(
        self,
        verdict: CriticVerdict,
        user_message: str,
        branches_used: list[str],
    ) -> None:
        kind = verdict.error_kind
        if kind is None:
            return
        embedding = self.embeddings.embed(user_message[:500])

        if kind == "sql_empty_result":
            try:
                self.nl2sql_memory.upsert_error(
                    failure_reason=f"Empty result for: {user_message[:160]}",
                    bad_pattern="(empty result)",
                    embedding=embedding,
                )
            except Exception as exc:
                log.warning("reflection.nl2sql_error_write_failed",
                            error=str(exc)[:120])
            return

        if kind in ("missing_branch", "wrong_branch_combo", "off_topic"):
            try:
                self.planner_memory.upsert_workflow_error(
                    question=user_message,
                    branches_used=branches_used,
                    error_kind=str(kind),
                    embedding=embedding,
                )
            except Exception as exc:
                log.warning("reflection.planner_error_write_failed",
                            error=str(exc)[:120])
            return

        if kind in ("citation_missing", "numeric_mismatch"):
            try:
                self.planner_memory.upsert_composition_error(
                    question=user_message,
                    error_kind=str(kind),
                    excerpt=verdict.notes or "",
                    embedding=embedding,
                )
            except Exception as exc:
                log.warning("reflection.composition_error_write_failed",
                            error=str(exc)[:120])
            return

        log.info("reflection.kind_unrouted", kind=kind)


def _json_dump(value: Any) -> str:
    import json
    try:
        return json.dumps(value or {}, default=str)
    except Exception:
        return "{}"
