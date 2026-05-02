"""
Bounded Planner v2 - redesign-2026-05 Phase 4.2.

Replaces the capability-template-based v1 planner. Design (PROJECT_REDESIGN_V2.md
1.2 + 5c):

    rewritten_query (Subtasks)
       -> Planner picks branches per subtask (Router-style decision)
       -> Workflow: which branches in parallel, which sequenced
       -> Execute each branch in parallel within the bounded DAG
       -> PlanExecutionV2 (per-branch outputs + errors + which exemplars used)

Bounds:
    - <= 3 branches running in parallel per subtask
    - <= 5 branch executions per workflow total (across all subtasks)

Q9 confidence rule (PROJECT_REDESIGN_V2.md 7c-B):
    - count_branch_combo_successes(branches) >= 3  -> high confidence;
                                                       skip Chroma 3 few-shot
    - else                                          -> recall few-shot

Few-shot is currently advisory (logged + carried in PlanExecutionV2.notes);
Phase 4.3 Report Writer doesn't need it directly because the Subtask carries
the suggested branches already. We keep the recall hook so Phase 5 can
back-propagate signal into Chroma 3.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import structlog
from pydantic import BaseModel, Field

from models.branch_output import (
    BranchExecutionStatus,
    EvidenceOutput,
    KGOutput,
    SQLOutput,
)
from models.query import RewrittenQuery, Subtask

log = structlog.get_logger(__name__)


BranchName = str  # "evidence" | "nl2sql" | "kg"


# ── Data contracts ──────────────────────────────────────────────────────────

class BranchInvocation(BaseModel):
    """One concrete branch call this Planner intends to run."""

    subtask_index: int
    branch: BranchName
    payload: dict = Field(default_factory=dict)


class BranchResult(BaseModel):
    invocation: BranchInvocation
    status: BranchExecutionStatus
    output: Optional[dict] = None  # branch_output dict (model_dump)


class PlanExecutionV2(BaseModel):
    workflow: list[BranchInvocation] = Field(default_factory=list)
    results: list[BranchResult] = Field(default_factory=list)
    branches_used: list[BranchName] = Field(default_factory=list)
    used_few_shot: bool = False
    notes: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0


@dataclass
class _BranchRouter:
    """Map subtask intent -> default branch set (Router responsibility).

    Multi-branch by default: most intents benefit from cross-source
    triangulation (raw counts from NL2SQL, who is amplifying it from KG,
    what authoritative sources said from Evidence). The Planner caps
    parallelism via `max_parallel_branches` (default 3) so even fan-out
    here stays bounded.
    """

    intent_branch_map: dict[str, list[BranchName]] = field(default_factory=lambda: {
        # Pure SQL aggregation - one branch is enough.
        "community_count":    ["nl2sql"],
        # Listing posts is helped by who-posted-with-whom context.
        "community_listing":  ["nl2sql", "kg"],
        # Trends are fundamentally multi-signal: counts + key spreaders.
        "trend":              ["nl2sql", "kg"],
        # Propagation always wants supporting volume from SQL alongside
        # the structural picture.
        "propagation":        ["kg", "nl2sql"],
        # Fact-check NEEDS official sources, but the community angle
        # (who is actually saying it on Reddit) is part of the answer.
        "fact_check":         ["evidence", "nl2sql"],
        # Recap an authoritative source. Add nl2sql so we can say
        # whether the community discussed it.
        "official_recap":     ["evidence", "nl2sql"],
        # Comparison is the canonical 3-source case.
        "comparison":         ["evidence", "nl2sql", "kg"],
        # Explaining a decision needs structural + factual.
        "explain_decision":   ["nl2sql", "kg"],
        # Freeform: cast a wide net.
        "freeform":           ["evidence", "nl2sql"],
        # ── KG-specialised (Phase C) ─────────────────────────────────────
        "propagation_trace":   ["kg"],
        "influencer_query":    ["kg", "nl2sql"],
        "coordination_check":  ["kg"],
        "community_structure": ["kg", "nl2sql"],
        "cascade_query":       ["kg"],
    })

    def route(self, subtask: Subtask) -> list[BranchName]:
        # Suggested branches from Query Rewriter take precedence
        if subtask.suggested_branches:
            return list(subtask.suggested_branches)
        return list(self.intent_branch_map.get(subtask.intent, ["evidence", "nl2sql"]))


# ── Planner ─────────────────────────────────────────────────────────────────

_TOPIC_SCOPED_INTENTS = {
    "community_count", "community_listing", "trend",
    "propagation", "explain_decision",
    # KG-specialised intents that benefit from semantic topic resolution
    "influencer_query", "coordination_check", "community_structure",
    "cascade_query",
}


# KG-specialised intent → (KG analytics method, default kwargs).
# `propagation_trace` is handled separately (it expects two account ids).
_KG_INTENT_TO_METHOD = {
    "influencer_query":    ("influencer_rank",    {}),
    "coordination_check":  ("coordinated_groups", {}),
    "community_structure": ("echo_chamber",       {}),
    "cascade_query":       ("viral_cascade",      {}),  # via KGQueryTool
    "propagation_trace":   ("propagation_path",   {}),  # via KGQueryTool
}


@dataclass
class BoundedPlannerV2:
    """Plans + executes branch calls for one rewritten query."""

    evidence_runner: Optional[Callable[[Subtask], EvidenceOutput]] = None
    nl2sql_runner: Optional[Callable[[Subtask], SQLOutput]] = None
    kg_runner: Optional[Callable[[Subtask], KGOutput]] = None

    planner_memory: Optional[Any] = None  # services.planner_memory.PlannerMemory
    embeddings: Optional[Any] = None      # services.embeddings_service.EmbeddingsService
    topic_resolver: Optional[Any] = None  # tools.topic_resolver.TopicResolver

    max_parallel_branches: int = 3
    max_branch_calls: int = 5
    confidence_hit_count: int = 3

    # ── Public ───────────────────────────────────────────────────────────────

    def plan_and_execute(self, rq: RewrittenQuery) -> PlanExecutionV2:
        t0 = time.monotonic()
        plan = self._plan(rq)
        execution = self._execute(plan, rq)
        execution.elapsed_ms = int((time.monotonic() - t0) * 1000)

        unique_branches: list[BranchName] = []
        for inv in execution.workflow:
            if inv.branch not in unique_branches:
                unique_branches.append(inv.branch)
        execution.branches_used = unique_branches

        log.info("planner_v2.done",
                 subtasks=len(rq.subtasks),
                 invocations=len(execution.workflow),
                 ok=sum(1 for r in execution.results if r.status.success),
                 fail=sum(1 for r in execution.results if not r.status.success),
                 used_few_shot=execution.used_few_shot,
                 elapsed_ms=execution.elapsed_ms)
        return execution

    # ── Planning ─────────────────────────────────────────────────────────────

    def _plan(self, rq: RewrittenQuery) -> list[BranchInvocation]:
        """Translate subtasks into branch invocations."""
        router = _BranchRouter()
        invocations: list[BranchInvocation] = []
        for idx, sub in enumerate(rq.subtasks):
            branches = router.route(sub)[: self.max_parallel_branches]
            # Pre-resolve semantic topic match for topic-scoped subtasks
            # so NL2SQL / KG get a concrete topic_id list to filter on.
            resolved_topic_ids = self._resolve_topics_for_subtask(sub)
            for branch in branches:
                if len(invocations) >= self.max_branch_calls:
                    break
                invocations.append(BranchInvocation(
                    subtask_index=idx,
                    branch=branch,
                    payload=self._build_payload(
                        branch, sub, resolved_topic_ids,
                    ),
                ))
            if len(invocations) >= self.max_branch_calls:
                break
        return invocations

    def _resolve_topics_for_subtask(self, sub: Subtask) -> list[dict]:
        """Run the semantic topic resolver. Returns list of {topic_id, label, similarity}."""
        # If the rewriter already pinned a specific topic_id, trust it.
        if sub.targets.topic_id:
            return [{
                "topic_id": sub.targets.topic_id,
                "label": "",
                "similarity": 1.0,
            }]
        # Only run for topic-scoped intents
        if sub.intent not in _TOPIC_SCOPED_INTENTS:
            return []
        if self.topic_resolver is None:
            try:
                from tools.topic_resolver import TopicResolver
                self.topic_resolver = TopicResolver()
            except Exception as exc:
                log.warning("planner_v2.topic_resolver_unavailable",
                            error=str(exc)[:160])
                return []
        try:
            matches = self.topic_resolver.resolve(sub.text, top_k=3)
        except Exception as exc:
            log.warning("planner_v2.topic_resolve_error",
                        error=str(exc)[:160])
            return []
        if not matches:
            return []
        log.info("planner_v2.topic_resolved",
                 phrase=sub.text[:80],
                 matches=[(m.topic_id, m.label, round(m.similarity, 3))
                          for m in matches])
        return [
            {"topic_id": m.topic_id, "label": m.label,
             "similarity": round(m.similarity, 3)}
            for m in matches
        ]

    @staticmethod
    def _build_payload(
        branch: BranchName, sub: Subtask,
        resolved_topic_ids: Optional[list[dict]] = None,
    ) -> dict:
        resolved_topic_ids = resolved_topic_ids or []
        topic_id_list = [t["topic_id"] for t in resolved_topic_ids]

        if branch == "evidence":
            return {
                "query": sub.text,
                "metadata_filter": sub.targets.metadata_filter,
            }
        if branch == "nl2sql":
            payload = {"nl_query": sub.text}
            if topic_id_list:
                # Append a structured hint that NL2SQL's prompt knows
                # to consume.
                payload["topic_id_hints"] = resolved_topic_ids
            return payload
        if branch == "kg":
            payload: dict[str, Any] = {"intent": sub.intent}
            # KG runner picks a single topic_id; use the top match.
            if topic_id_list:
                payload["topic_id"] = topic_id_list[0]
            elif sub.targets.topic_id:
                payload["topic_id"] = sub.targets.topic_id
            if sub.targets.account_id:
                payload["account_id"] = sub.targets.account_id
            # propagation_trace needs two account anchors; the rewriter
            # may stash them in metadata_filter or in the subtask text.
            if sub.intent == "propagation_trace":
                meta = sub.targets.metadata_filter or {}
                if meta.get("source_account"):
                    payload["source_account"] = meta["source_account"]
                if meta.get("target_account"):
                    payload["target_account"] = meta["target_account"]
            return payload
        return {}

    # ── Execution ────────────────────────────────────────────────────────────

    def _execute(
        self, plan: list[BranchInvocation], rq: RewrittenQuery,
    ) -> PlanExecutionV2:
        execution = PlanExecutionV2(workflow=list(plan))
        if not plan:
            execution.notes.append("empty_plan")
            return execution

        # Q9 confidence rule + few-shot recall (advisory)
        unique_branches = sorted({inv.branch for inv in plan})
        execution.used_few_shot = self._should_use_few_shot(
            rq, unique_branches,
        )

        # Parallel execution. Each branch is independent at the contract
        # level (they read different stores).
        with ThreadPoolExecutor(
            max_workers=self.max_parallel_branches,
        ) as pool:
            futures = {
                pool.submit(self._run_one, inv): inv
                for inv in plan
            }
            for fut in as_completed(futures):
                inv = futures[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    log.error("planner_v2.branch_exception",
                              branch=inv.branch, error=str(exc)[:160])
                    result = BranchResult(
                        invocation=inv,
                        status=BranchExecutionStatus(
                            branch=inv.branch, success=False,
                            error=str(exc)[:200],
                            error_kind="branch_exception",
                        ),
                    )
                execution.results.append(result)

        # Restore stable order (by original plan index)
        order = {id(inv): i for i, inv in enumerate(plan)}
        execution.results.sort(key=lambda r: order.get(id(r.invocation), 0))
        return execution

    def _should_use_few_shot(
        self, rq: RewrittenQuery, unique_branches: list[BranchName],
    ) -> bool:
        if self.planner_memory is None or self.embeddings is None:
            return False
        try:
            hits = self.planner_memory.count_branch_combo_successes(unique_branches)
            if hits >= self.confidence_hit_count:
                return False
            # Low confidence: recall a few exemplars (advisory)
            embedding = self.embeddings.embed(rq.original)
            self.planner_memory.recall_workflow_exemplars(
                embedding, n_results=5,
            )
            return True
        except Exception as exc:
            log.warning("planner_v2.few_shot_recall_error",
                        error=str(exc)[:120])
            return False

    def _run_one(self, inv: BranchInvocation) -> BranchResult:
        t0 = time.monotonic()
        runner = self._select_runner(inv.branch)
        if runner is None:
            return BranchResult(
                invocation=inv,
                status=BranchExecutionStatus(
                    branch=inv.branch, success=False,
                    error=f"no runner registered for branch {inv.branch}",
                    error_kind="branch_missing",
                ),
            )
        try:
            output_obj = runner(inv)
            elapsed = int((time.monotonic() - t0) * 1000)
            return BranchResult(
                invocation=inv,
                status=BranchExecutionStatus(
                    branch=inv.branch, success=True, elapsed_ms=elapsed,
                ),
                output=output_obj.model_dump() if hasattr(
                    output_obj, "model_dump") else output_obj,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            log.error("planner_v2.runner_error",
                      branch=inv.branch, error=str(exc)[:160])
            return BranchResult(
                invocation=inv,
                status=BranchExecutionStatus(
                    branch=inv.branch, success=False,
                    error=str(exc)[:200], error_kind="branch_runner_error",
                    elapsed_ms=elapsed,
                ),
            )

    def _select_runner(self, branch: BranchName):
        if branch == "evidence":
            return self.evidence_runner
        if branch == "nl2sql":
            return self.nl2sql_runner
        if branch == "kg":
            return self.kg_runner
        return None


# ── Default branch runners ──────────────────────────────────────────────────
# These thin wrappers map (BranchInvocation) -> (EvidenceOutput | SQLOutput |
# KGOutput) using the Phase 3 tools. Tests / unusual deployments can pass in
# their own runners.

def default_evidence_runner(inv: BranchInvocation) -> EvidenceOutput:
    from tools.hybrid_retrieval import HybridRetriever
    bundle = HybridRetriever().retrieve(
        query=inv.payload.get("query", ""),
        metadata_filter=inv.payload.get("metadata_filter") or None,
    )
    return EvidenceOutput(bundle=bundle)


def default_nl2sql_runner(inv: BranchInvocation) -> SQLOutput:
    from tools.nl2sql_tools import NL2SQLTool
    return NL2SQLTool().answer(
        inv.payload.get("nl_query", ""),
        topic_id_hints=inv.payload.get("topic_id_hints"),
    )


def default_kg_runner(inv: BranchInvocation) -> KGOutput:
    """Dispatch a KG branch invocation to the right tool.

    Routing (Phase C):
      propagation_trace   -> tools.kg_query_tools.propagation_path
      cascade_query       -> tools.kg_query_tools.viral_cascade
      influencer_query    -> agents.kg_analytics.influencer_rank (PageRank)
      coordination_check  -> agents.kg_analytics.coordinated_groups (Louvain)
      community_structure -> agents.kg_analytics.echo_chamber (modularity)
      propagation         -> coordinated_groups (legacy alias)
      anything else       -> influencer_rank as a sensible default
    """
    from tools.kg_query_tools import KGQueryTool

    intent = inv.payload.get("intent", "freeform")
    topic_id = inv.payload.get("topic_id") or ""

    # Anchor-less queries: try to back-fill from the most-discussed topic so
    # KG actually produces signal.
    if not topic_id and intent not in {
        "propagation_trace", "coordination_check",
    }:
        topic_id = _resolve_default_topic_id() or ""

    # ── propagation_trace (KGQueryTool) ──────────────────────────────────────
    if intent == "propagation_trace":
        src = inv.payload.get("source_account") or ""
        dst = inv.payload.get("target_account") or ""
        if not src or not dst:
            from models.branch_output import KGOutput as _KGOut
            return _KGOut(
                query_kind="propagation_path",
                target={"reason": "missing source/target account"},
            )
        return KGQueryTool().propagation_path(
            source_account=src, target_account=dst,
            max_hops=int(inv.payload.get("max_hops", 6)),
        )

    # ── cascade_query (KGQueryTool) ──────────────────────────────────────────
    if intent == "cascade_query":
        return KGQueryTool().viral_cascade(
            topic_id=topic_id,
            top_k=int(inv.payload.get("top_k", 5)),
        )

    # ── KGAnalytics methods (Phase B.3) ──────────────────────────────────────
    from agents.kg_analytics import KGAnalytics
    analytics = KGAnalytics()

    if intent == "influencer_query":
        return analytics.influencer_rank(
            topic_id=topic_id or None,
            top_k=int(inv.payload.get("top_k", 10)),
        )
    if intent in ("coordination_check", "propagation"):
        return analytics.coordinated_groups(
            topic_id=topic_id or None,
            min_size=int(inv.payload.get("min_size", 3)),
        )
    if intent == "community_structure":
        return analytics.echo_chamber(
            topic_id=topic_id,
            modularity_threshold=float(
                inv.payload.get("modularity_threshold", 0.3),
            ),
        )

    # Fallback: PageRank gives a more useful default than raw post counts.
    if not topic_id:
        from models.branch_output import KGOutput as _KGOut
        return _KGOut(query_kind="influencer_rank",
                       target={"reason": "no topic anchor available"})
    return analytics.influencer_rank(topic_id=topic_id, top_k=10)


def _resolve_default_topic_id() -> str | None:
    """Best-effort: return the most-discussed topic_id from PG."""
    try:
        from services.postgres_service import PostgresService
        pg = PostgresService()
        pg.connect()
        with pg.cursor() as cur:
            cur.execute(
                "SELECT topic_id FROM topics_v2 "
                "ORDER BY post_count DESC LIMIT 1"
            )
            row = cur.fetchone()
            return row.get("topic_id") if row else None
    except Exception:
        return None
