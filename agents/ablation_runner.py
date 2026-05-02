"""
Ablation runner - redesign-2026-05 Phase 5.2.

When the Quality Critic rejects a report and `causal_record_ids` are
attached (records pulled from Chroma 2 / Chroma 3 during the failed turn),
the Reflection store needs to know which of those records is actually the
poison. The ablation runner answers that by:

    1. Re-running the same workflow without the suspect record(s).
    2. Re-checking the Critic.
    3. Returning True if the failure disappeared (i.e. the record was the
       cause).

Cost cap: at most 3 suspect records per Critic failure. The Critic call
is itself bounded so the worst case is 3 extra Critic checks plus 3 extra
Writer/Planner runs per failed turn.

Phase 5 ships a structural implementation that depends only on the
collaborator handles passed in by the caller. ChatOrchestrator wires it
to Reflection in this same phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import structlog

from agents.planner_v2 import BoundedPlannerV2, PlanExecutionV2
from agents.quality_critic import QualityCritic
from agents.report_writer import ReportWriter
from models.query import RewrittenQuery
from models.reflection import CriticVerdict
from services.nl2sql_memory import NL2SQLMemory
from services.planner_memory import PlannerMemory

log = structlog.get_logger(__name__)


@dataclass
class AblationContext:
    """Snapshot the Orchestrator passes per failing turn."""

    rewritten: RewrittenQuery
    execution: PlanExecutionV2
    planner: BoundedPlannerV2
    writer: ReportWriter
    critic: QualityCritic
    nl2sql_memory: Optional[NL2SQLMemory] = None
    planner_memory: Optional[PlannerMemory] = None


@dataclass
class AblationRunner:
    """Callable form usable as `ReflectionStore.ablation_runner`.

    Reflection passes us a `CriticVerdict` (which carries causal_record_ids)
    and the list of ids it wants tested. We replay the workflow with those
    ids "muted" and report whether the new run passes Critic.
    """

    context: AblationContext

    def __call__(
        self, verdict: CriticVerdict, suspect_ids: list[str],
    ) -> bool:
        if not suspect_ids:
            return False

        ctx = self.context
        suspect_set = set(suspect_ids)

        with _muted_records(ctx.nl2sql_memory, ctx.planner_memory, suspect_set):
            try:
                # Replay planner + writer + critic
                replay_execution = ctx.planner.plan_and_execute(ctx.rewritten)
                replay_report = ctx.writer.write(ctx.rewritten, replay_execution)
                replay_verdict = ctx.critic.review(replay_report, replay_execution)
            except Exception as exc:
                log.warning("ablation.replay_error", error=str(exc)[:160])
                return False

        if replay_verdict.passed:
            log.info("ablation.guilty_records_found",
                     suspects=suspect_ids,
                     prior_kind=verdict.error_kind)
            return True
        return False


# ── Suspension helpers ──────────────────────────────────────────────────────

class _muted_records:
    """Context manager that filters out the suspect ids from memory recalls.

    We don't actually delete the docs (Reflection might decide not to drop
    them after this test). Instead we patch the memory's recall method to
    return the same docs minus the suspects for the duration of the
    replay.
    """

    def __init__(
        self,
        nl_memory: Optional[NL2SQLMemory],
        planner_memory: Optional[PlannerMemory],
        suspects: set[str],
    ) -> None:
        self._nl_memory = nl_memory
        self._planner_memory = planner_memory
        self._suspects = suspects
        self._saved_nl: dict = {}
        self._saved_planner: dict = {}

    def __enter__(self) -> "_muted_records":
        if self._nl_memory is not None:
            for attr in ("recall_schema", "recall_success", "recall_errors"):
                fn = getattr(self._nl_memory, attr, None)
                if fn is None:
                    continue
                self._saved_nl[attr] = fn
                setattr(
                    self._nl_memory, attr,
                    _wrap_recall(fn, self._suspects),
                )
        if self._planner_memory is not None:
            for attr in (
                "recall_module_cards",
                "recall_workflow_exemplars",
                "recall_workflow_errors",
            ):
                fn = getattr(self._planner_memory, attr, None)
                if fn is None:
                    continue
                self._saved_planner[attr] = fn
                setattr(
                    self._planner_memory, attr,
                    _wrap_recall(fn, self._suspects),
                )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for attr, fn in self._saved_nl.items():
            setattr(self._nl_memory, attr, fn)
        for attr, fn in self._saved_planner.items():
            setattr(self._planner_memory, attr, fn)


def _wrap_recall(fn: Callable, suspects: set[str]) -> Callable:
    """Filter the recall return-value to drop suspect ids."""
    def wrapped(*args, **kwargs):
        results = fn(*args, **kwargs) or []
        if not isinstance(results, list):
            return results
        return [r for r in results
                if not (isinstance(r, dict) and r.get("id") in suspects)]
    return wrapped
