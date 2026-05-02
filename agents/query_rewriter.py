"""
Query Rewriter - redesign-2026-05 Phase 4.1.

Step 2 of the front-end pipeline (PROJECT_REDESIGN_V2.md 1.2):

    1. user question
    2. -> Query Rewrite / subtask split / context resolution
    3. -> Router (per-subtask)
    4. -> Planner

Responsibilities:
- Split compound questions ("compare A and B...") into independent subtasks
  the Planner can run in parallel.
- Resolve pronouns / "this topic" / "that claim" against the running
  session state (current_run_id / current_topic_id / current_claim_id).
- Suggest a `BranchSet` per subtask so the Router has a starting point.
  The Router still has the final say.

Implementation: a single LLM call returning STRICT JSON. Failure modes:
- LLM error / malformed JSON -> degrade to a single freeform subtask
  carrying the original text and the inherited session context.
"""
from __future__ import annotations

import json
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.query import (
    RewrittenQuery,
    Subtask,
    SubtaskTarget,
)
from models.session import SessionState

log = structlog.get_logger(__name__)


_REWRITE_SYSTEM = """You rewrite a user's question for a downstream multi-source
analysis system. Return STRICT JSON with this shape:

{
  "subtasks": [
    {
      "text": "self-contained rewritten question",
      "intent": "fact_check | official_recap | community_count | community_listing | trend | propagation | comparison | explain_decision | freeform | propagation_trace | influencer_query | coordination_check | community_structure | cascade_query",
      "suggested_branches": ["evidence" | "nl2sql" | "kg", ...],
      "targets": {
        "run_id": null,
        "topic_id": null,
        "claim_id": null,
        "account_id": null,
        "timeframe": null,
        "metadata_filter": {}
      },
      "rationale": "one sentence on why this split / branch suggestion"
    }
  ]
}

Rules:
- 1-3 subtasks. Don't over-split simple questions.
- Resolve pronouns using the provided session_context (current_run_id /
  current_topic_id / current_claim_id). Insert the resolved id into targets.
- "this topic" / "that one" -> use session_context.current_topic_id
- "this claim" -> use session_context.current_claim_id
- "compare A and B" -> two subtasks plus optionally a 3rd "comparison" subtask
  using "comparison" intent and both branches the children used.

KG-specialised intents (KEY UPGRADE; pick these whenever applicable):
  - propagation_trace   : "show how this rumour spread" / "trace the reply chain" /
                          "path from A to B" / "did X end up replying to Y" ->
                          KG only (multi-hop reply traversal; SQL can't do it)
                          IMPORTANT: extract the two account ids and put
                          them under targets.metadata_filter as
                          {"source_account": "<id>", "target_account": "<id>"}.
                          The runner cannot query without both. Account
                          ids in this system look like "u_alice", "u_bob",
                          or any handle the user provides verbatim.
  - influencer_query    : "who is most influential" / "top spreaders" /
                          "amplifiers" / "key opinion leaders" ->
                          KG (PageRank, not post count) + nl2sql for context
  - coordination_check  : "is this organised" / "coordinated posting" /
                          "bot network" / "is there a campaign" ->
                          KG only (Louvain community detection)
  - community_structure : "echo chamber" / "are these in the same group" /
                          "cluster" / "polarised" ->
                          KG (modularity) + nl2sql for who-is-where
  - cascade_query       : "viral" / "longest thread" / "deepest reply chain" /
                          "what spread furthest" / "cascade size" ->
                          KG only (cascade tree, viral_cascade ranking)

Why KG-specialised: PageRank, betweenness, Louvain, k-hop reply paths, and
cascade trees CANNOT be expressed in SQL. Routing these to nl2sql produces
shallow GROUP-BY answers that miss the structure of the spread.

- suggested_branches: prefer MULTI-BRANCH unless the question is a clear
  single-aggregation SQL job. Default mappings:
    fact_check                  -> ["evidence", "nl2sql"]
    official_recap              -> ["evidence", "nl2sql"]
    community_count             -> ["nl2sql"]      (pure aggregation)
    community_listing           -> ["nl2sql", "kg"]
    trend                       -> ["nl2sql", "kg"]
    propagation                 -> ["kg", "nl2sql"]
    comparison                  -> ["evidence", "nl2sql", "kg"]
    explain_decision            -> ["nl2sql", "kg"]
    freeform                    -> ["evidence", "nl2sql"]
    # KG-specialised:
    propagation_trace           -> ["kg"]
    influencer_query            -> ["kg", "nl2sql"]
    coordination_check          -> ["kg"]
    community_structure         -> ["kg", "nl2sql"]
    cascade_query               -> ["kg"]
  Keep at most 3 branches per subtask.
- targets.metadata_filter examples: {"tier": "reputable_media"} or
  {"source": "bbc"} for evidence; leave empty by default.
- Do NOT invent IDs. If you cannot resolve, leave the field null.
- Output STRICT JSON only. No prose. No markdown fences.
"""


class QueryRewriter:
    def __init__(
        self,
        client: Optional[openai.OpenAI] = None,
        model: str = OPENAI_MODEL,
    ) -> None:
        self._client = client or openai.OpenAI(api_key=OPENAI_API_KEY)
        self._model = model

    # ── Public ─────────────────────────────────────────────────────────────────

    def rewrite(
        self,
        message: str,
        session: Optional[SessionState] = None,
    ) -> RewrittenQuery:
        ctx = self._session_context(session)
        if not message or not message.strip():
            return RewrittenQuery(
                original=message or "",
                subtasks=[Subtask(text=message or "", intent="freeform")],
                inherited_context=ctx,
                fallback_reason="empty_message",
            )

        try:
            data = self._call_llm(message, ctx)
        except Exception as exc:
            log.error("query_rewriter.llm_error", error=str(exc)[:120])
            return self._degraded(message, ctx,
                                  fallback_reason=f"llm_error: {exc}")

        subtasks = self._parse_subtasks(data, ctx)
        if not subtasks:
            return self._degraded(message, ctx,
                                  fallback_reason="no_subtasks_returned")

        rq = RewrittenQuery(
            original=message,
            subtasks=subtasks,
            inherited_context=ctx,
        )
        log.info("query_rewriter.done",
                 subtasks=len(rq.subtasks),
                 multistep=rq.is_multistep,
                 intents=[s.intent for s in rq.subtasks])
        return rq

    # ── Internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _session_context(session: Optional[SessionState]) -> dict:
        if session is None:
            return {}
        ctx: dict = {
            "current_run_id": session.current_run_id,
            "current_topic_id": session.current_topic_id,
            "current_claim_id": session.current_claim_id,
            "recent_assistants": [
                {"capability_used": t.capability_used,
                 "preview": (t.content or "")[:120]}
                for t in (session.conversation or [])[-3:]
                if t.role == "assistant"
            ],
        }
        # Phase 6 (B): if older turns were compressed into a rolling
        # summary, surface it so the Rewriter can resolve pronouns or
        # earlier topic anchors that fall outside the live window.
        if session.summary:
            ctx["older_context_summary"] = session.summary
            ctx["archived_turns"] = session.archived_count
        return ctx

    def _call_llm(self, message: str, ctx: dict) -> dict:
        user_msg = (
            f"Session context: {json.dumps(ctx, ensure_ascii=False)}\n"
            f"User message: {message}"
        )
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=512,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _REWRITE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (resp.choices[0].message.content or "{}").strip()
        return json.loads(raw)

    def _parse_subtasks(self, data: dict, ctx: dict) -> list[Subtask]:
        items = data.get("subtasks") or []
        out: list[Subtask] = []
        valid_branches = {"evidence", "nl2sql", "kg"}
        valid_intents = {
            "fact_check", "official_recap", "community_count",
            "community_listing", "trend", "propagation",
            "comparison", "explain_decision", "freeform",
            # Phase C KG-specialised
            "propagation_trace", "influencer_query",
            "coordination_check", "community_structure",
            "cascade_query",
        }
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            intent = item.get("intent") or "freeform"
            if intent not in valid_intents:
                intent = "freeform"

            branches_raw = item.get("suggested_branches") or []
            branches = [b for b in branches_raw if b in valid_branches]

            target_raw = item.get("targets") or {}
            targets = SubtaskTarget(
                run_id=target_raw.get("run_id") or ctx.get("current_run_id"),
                topic_id=target_raw.get("topic_id") or ctx.get("current_topic_id"),
                claim_id=target_raw.get("claim_id") or ctx.get("current_claim_id"),
                account_id=target_raw.get("account_id"),
                timeframe=target_raw.get("timeframe"),
                metadata_filter=target_raw.get("metadata_filter") or {},
            )
            out.append(Subtask(
                text=text,
                intent=intent,
                suggested_branches=branches,
                targets=targets,
                rationale=item.get("rationale", ""),
            ))
        return out

    @staticmethod
    def _degraded(
        message: str, ctx: dict, fallback_reason: str,
    ) -> RewrittenQuery:
        return RewrittenQuery(
            original=message,
            subtasks=[Subtask(
                text=message,
                intent="freeform",
                targets=SubtaskTarget(
                    run_id=ctx.get("current_run_id"),
                    topic_id=ctx.get("current_topic_id"),
                    claim_id=ctx.get("current_claim_id"),
                ),
                rationale="rewrite_failed; passthrough",
            )],
            inherited_context=ctx,
            fallback_reason=fallback_reason,
        )
