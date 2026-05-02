"""Chat Orchestrator (v2) - redesign-2026-05 Phase 4.5.

Single entry point for `POST /chat/query`. Pipeline (PROJECT_REDESIGN_V2.md
section 1.2):

    load session state
       -> QueryRewriter.rewrite(message, session)        # subtask split
       -> BoundedPlannerV2.plan_and_execute(rq)          # branch DAG
       -> ReportWriter.write(rq, execution)              # markdown report
       -> QualityCritic.review(report, execution)        # 4-axis check
       -> retry once on critic failure; flag for human review on 2nd
       -> ReflectionStore.record(verdict, ...)           # auto-curate
       -> append turn to session, return ChatResponse

The Orchestrator owns no business logic. Capabilities (v1) are gone from
this code path; the v2 link reads only the three branches.

Backwards compatibility:
- The wire-format `ChatResponse` keeps `capability_used` populated with
  the dominant branch name so existing UI components don't crash.
- Session JSON gains a `branches_used` field on each turn (Phase 4.5
  session model bump).
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

import structlog

from agents.ablation_runner import AblationContext, AblationRunner
from agents.planner_v2 import (
    BoundedPlannerV2,
    PlanExecutionV2,
    default_evidence_runner,
    default_kg_runner,
    default_nl2sql_runner,
)
from agents.query_rewriter import QueryRewriter
from agents.quality_critic import QualityCritic
from agents.report_writer import ReportWriter
from models.chat import ChatResponse
from models.query import RewrittenQuery
from models.reflection import CriticVerdict
from models.report_v2 import ReportV2
from models.session import SessionState
from services import session_store
from services.embeddings_service import EmbeddingsService
from services.nl2sql_memory import NL2SQLMemory
from services.planner_memory import PlannerMemory
from services.reflection_store import ReflectionStore

log = structlog.get_logger(__name__)


class ChatOrchestrator:
    """v2 chat orchestrator. Owns wiring, owns nothing else."""

    def __init__(
        self,
        rewriter: Optional[QueryRewriter] = None,
        planner: Optional[BoundedPlannerV2] = None,
        writer: Optional[ReportWriter] = None,
        critic: Optional[QualityCritic] = None,
        reflection: Optional[ReflectionStore] = None,
    ) -> None:
        self._rewriter = rewriter or QueryRewriter()
        self._planner = planner or BoundedPlannerV2(
            evidence_runner=default_evidence_runner,
            nl2sql_runner=default_nl2sql_runner,
            kg_runner=default_kg_runner,
            planner_memory=PlannerMemory(),
            embeddings=EmbeddingsService(),
        )
        self._writer = writer or ReportWriter()
        self._critic = critic or QualityCritic()
        self._reflection = reflection or ReflectionStore()

    # ── Public ───────────────────────────────────────────────────────────────

    def handle(self, session_id: str, message: str) -> ChatResponse:
        state = session_store.load(session_id or _new_session_id())
        session_store.append_turn(state, role="user", content=message)

        # 1. Rewrite + subtask split
        rq = self._rewriter.rewrite(message, state)

        # 2. Plan + execute branch DAG
        execution = self._planner.plan_and_execute(rq)

        # 3. Compose + Critic loop (single retry)
        report, verdict = self._compose_with_critic(rq, execution)

        # 4. Reflection (audit + auto-removal)
        # Phase 5.2: install a real ablation runner scoped to this turn.
        try:
            self._install_ablation(rq, execution)
            self._reflection.record(
                verdict,
                user_message=message,
                session_id=state.session_id,
                branches_used=execution.branches_used,
                payload={"workflow": [
                    inv.model_dump() for inv in execution.workflow
                ]},
            )
        except Exception as exc:
            log.warning("orchestrator.reflection_record_error",
                        error=str(exc)[:160])
        finally:
            self._reflection.ablation_runner = (lambda v, ids: False)  # reset

        # 5. Persist + respond
        dominant_branch = execution.branches_used[0] if execution.branches_used else None
        session_store.append_turn(
            state, role="assistant",
            content=report.markdown_body,
            capability_used=dominant_branch,
            branches_used=list(execution.branches_used),
        )
        self._update_targets(state, rq, execution)
        session_store.save(state)

        return ChatResponse(
            session_id=state.session_id,
            answer_text=report.markdown_body,
            capability_used=dominant_branch,
            capability_output=_legacy_capability_output(execution),
            visual_paths=[],
            branches_used=list(execution.branches_used),
            branch_outputs=_branch_outputs_payload(execution),
            citations=[c.model_dump() for c in report.citations],
            needs_human_review=report.needs_human_review,
        )

    # ── Internals ────────────────────────────────────────────────────────────

    def _compose_with_critic(
        self, rq: RewrittenQuery, execution: PlanExecutionV2,
    ) -> tuple[ReportV2, CriticVerdict]:
        report = self._writer.write(rq, execution)
        verdict = self._critic.review(report, execution)
        if verdict.passed:
            return report, verdict

        log.info("orchestrator.critic_failed_first",
                 error_kind=verdict.error_kind,
                 failed_branch=verdict.failed_branch)
        # Retry: rewrite once more with the previous attempt as context
        report_retry = self._writer.write(rq, execution)
        verdict_retry = self._critic.review(report_retry, execution)
        if verdict_retry.passed:
            return report_retry, verdict_retry

        log.warning("orchestrator.critic_failed_second",
                    error_kind=verdict_retry.error_kind)
        # Degraded: flag for human review, keep the second body
        report_retry.needs_human_review = True
        report_retry.notes.append(
            f"critic_failed: {verdict_retry.error_kind}",
        )
        return report_retry, verdict_retry

    def _install_ablation(
        self, rq: RewrittenQuery, execution: PlanExecutionV2,
    ) -> None:
        """Plug the per-turn AblationRunner into ReflectionStore."""
        ctx = AblationContext(
            rewritten=rq,
            execution=execution,
            planner=self._planner,
            writer=self._writer,
            critic=self._critic,
            nl2sql_memory=getattr(self._reflection, "nl2sql_memory", None),
            planner_memory=getattr(self._reflection, "planner_memory", None),
        )
        self._reflection.ablation_runner = AblationRunner(context=ctx)

    @staticmethod
    def _update_targets(
        state: SessionState, rq: RewrittenQuery,
        execution: PlanExecutionV2,
    ) -> None:
        # Inherit targets from the rewriter's first subtask, in priority order
        for sub in rq.subtasks:
            if sub.targets.run_id:
                state.current_run_id = sub.targets.run_id
            if sub.targets.topic_id:
                state.current_topic_id = sub.targets.topic_id
            if sub.targets.claim_id:
                state.current_claim_id = sub.targets.claim_id


# ── Helpers ──────────────────────────────────────────────────────────────────

def _new_session_id() -> str:
    return f"s-{uuid.uuid4().hex[:12]}"


def _branch_outputs_payload(execution: PlanExecutionV2) -> dict[str, Any]:
    """Group branch outputs by branch name for UI rendering."""
    grouped: dict[str, list[Any]] = {}
    for r in execution.results:
        if not r.status.success or r.output is None:
            continue
        grouped.setdefault(r.invocation.branch, []).append(r.output)
    return grouped


def _legacy_capability_output(execution: PlanExecutionV2) -> dict[str, Any]:
    """Best-effort v1-shaped capability_output so existing UI code still works.

    The v1 chat path used a single `capability_output: dict`. We collapse the
    v2 multi-branch result into the same shape by exposing the dominant
    branch's output under `primary` and the rest under `aux_outputs`.
    """
    grouped = _branch_outputs_payload(execution)
    if not grouped:
        return {}
    # Pick a primary branch deterministically (first in execution.branches_used)
    primary = execution.branches_used[0] if execution.branches_used else None
    payload: dict[str, Any] = {
        "branches_used": list(execution.branches_used),
    }
    if primary and primary in grouped and grouped[primary]:
        payload["primary"] = grouped[primary][0]
    aux = {k: v for k, v in grouped.items() if k != primary}
    if aux:
        payload["aux_outputs"] = aux
    return payload
