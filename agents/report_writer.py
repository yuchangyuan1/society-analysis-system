"""
Report Writer - redesign-2026-05 Phase 4.3.

Replaces v1's `services/answer_composer.py` for the v2 chat path. Composes
a markdown report from the aggregated branch outputs (evidence / nl2sql /
kg) produced by `agents/planner_v2.py`.

Design constraints (PROJECT_REDESIGN_V2.md 1.2):
- Only ONE LLM call per invocation. The system prompt forbids inventing
  facts: every sentence with a number must cite a row from `nl2sql.rows`
  or a chunk_id from the evidence bundle.
- Outputs `ReportV2` (markdown_body + citations + numbers). Numbers are
  duplicated to a structured table so the Quality Critic can verify them
  programmatically.
- If the planner failed to produce any branch output, we degrade to a
  short "I couldn't gather enough data" fallback rather than hallucinating.
- Post-processes the markdown to swap raw `topic_*` / `ent_*` ids for
  human-readable labels/names by looking up topics_v2 / entities_v2.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.branch_output import EvidenceOutput, KGOutput, SQLOutput
from models.evidence import Citation
from models.query import RewrittenQuery
from models.report_v2 import ReportNumber, ReportV2
from agents.planner_v2 import PlanExecutionV2

log = structlog.get_logger(__name__)


_TOPIC_ID_RE = re.compile(r"\btopic_[0-9a-f]{6,}\b")
_ENTITY_ID_RE = re.compile(r"\bent_[0-9a-f]{6,}\b")


_WRITER_SYSTEM = """You compose a concise markdown answer for a research user.
You will receive structured outputs from up to three retrieval branches:
  - evidence:  authoritative-source chunks with chunk_ids and citations
  - nl2sql:    SQL rows from the community Postgres store
  - kg:        graph nodes / edges / metrics from Kuzu

Your output JSON shape:
{
  "markdown_body": "...",
  "numbers": [
    {"label": "post_count", "value": 42, "source_branch": "nl2sql",
     "source_ref": "<sql snippet or column>"},
    ...
  ]
}

Hard rules:
- Every numerical claim in the markdown MUST also appear in the numbers
  array, with source_branch matching the actual source.
- Cite evidence chunks inline as `[chunk_id]` whenever you reference a
  fact attributed to an official source.
- If the user asks something the data doesn't cover, say so explicitly.
- Do NOT invent SQL rows, account ids, or chunk ids.
- Keep the report under 300 words. Use bullet points for lists.
- Output STRICT JSON only. No prose. No code fences.

Graph (KG) presentation rules:
- KG node ids look like `rc_b1`, `topic_xxx`, `ent_xxx` etc. They are
  internal identifiers - DO NOT show them in the markdown.
- For Post nodes use the `author` and `text` properties. Quote the text
  excerpt directly (truncated as provided) and credit the `author`.
  Format: "_<author>_: \"<text>\"" with the body in quotes.
- For Account nodes use the id ONLY when the user explicitly mentioned
  that handle (e.g. "u_alice"). Otherwise prefer the username/handle if
  one is in `properties`.
- For reply chains, write a flowing sentence like "alice's post about X
  was replied to by bob (\"...\"), then by carol (\"...\")" rather than
  a bare list of post ids.
- For PageRank / Louvain / centrality: name the people, summarise their
  scores in plain language ("most influential", "central bridge",
  "tight cluster of N accounts"), and quote one or two characteristic
  posts when post-level data is in the bundle.
"""


class ReportWriter:
    def __init__(
        self,
        client: Optional[openai.OpenAI] = None,
        model: str = OPENAI_MODEL,
    ) -> None:
        self._client = client or openai.OpenAI(api_key=OPENAI_API_KEY)
        self._model = model

    # ── Public ─────────────────────────────────────────────────────────────────

    def write(
        self,
        rq: RewrittenQuery,
        execution: PlanExecutionV2,
    ) -> ReportV2:
        report = ReportV2(
            user_question=rq.original,
            branches_used=list(execution.branches_used),
        )

        # Aggregate outputs by branch
        evidence_outs = _filter_outputs(execution, "evidence", EvidenceOutput)
        sql_outs = _filter_outputs(execution, "nl2sql", SQLOutput)
        kg_outs = _filter_outputs(execution, "kg", KGOutput)

        # Hoist all citations even if the LLM omits them in prose
        for ev in evidence_outs:
            for chunk in ev.bundle.chunks:
                report.citations.append(chunk.citation)
        report.citations = _dedupe_citations(report.citations)

        if not (evidence_outs or sql_outs or kg_outs):
            report.markdown_body = (
                "I couldn't gather enough data to answer that. "
                "Please refine the question or check the run targeted."
            )
            report.notes.append("no_branch_output")
            return report

        # Build the LLM payload
        user_payload = self._build_payload(
            rq, execution, evidence_outs, sql_outs, kg_outs,
        )
        try:
            data = self._call_llm(user_payload)
        except Exception as exc:
            log.error("report_writer.llm_error", error=str(exc)[:160])
            report.markdown_body = self._fallback_body(
                evidence_outs, sql_outs, kg_outs,
            )
            report.notes.append(f"llm_error: {exc}")
            return report

        body = (data.get("markdown_body") or "").strip()
        numbers_raw = data.get("numbers") or []

        if not body:
            report.markdown_body = self._fallback_body(
                evidence_outs, sql_outs, kg_outs,
            )
            report.notes.append("empty_markdown_body")
            return report

        # Post-process: swap raw topic/entity ids for human labels.
        body = self._humanize_ids(body)

        report.markdown_body = body
        for n in numbers_raw:
            if not isinstance(n, dict):
                continue
            try:
                report.numbers.append(ReportNumber(
                    label=str(n.get("label") or "")[:80],
                    value=float(n.get("value") or 0.0),
                    source_branch=str(n.get("source_branch") or "")[:20],
                    source_ref=(str(n.get("source_ref"))[:160]
                                if n.get("source_ref") is not None else None),
                ))
            except (TypeError, ValueError):
                continue

        log.info("report_writer.done",
                 branches=report.branches_used,
                 chars=len(report.markdown_body),
                 numbers=len(report.numbers),
                 citations=len(report.citations))
        return report

    # ── Internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_payload(
        rq: RewrittenQuery,
        execution: PlanExecutionV2,
        evidence_outs: list[EvidenceOutput],
        sql_outs: list[SQLOutput],
        kg_outs: list[KGOutput],
    ) -> str:
        sections: list[str] = [f"User question: {rq.original}"]

        if rq.subtasks:
            sections.append("Subtasks:")
            for i, s in enumerate(rq.subtasks, start=1):
                sections.append(
                    f"  {i}. ({s.intent}) {s.text} "
                    f"[branches: {','.join(s.suggested_branches) or 'auto'}]"
                )

        if evidence_outs:
            sections.append("Evidence chunks:")
            for ev in evidence_outs:
                for c in ev.bundle.chunks[:8]:
                    sections.append(
                        f"  - [{c.chunk_id}] ({c.citation.source}): "
                        f"{c.text[:300]}"
                    )

        if sql_outs:
            sections.append("SQL outputs:")
            for s in sql_outs:
                if not s.success:
                    sections.append(
                        f"  - [SQL FAILED] attempts={len(s.attempts)} "
                        f"error_kind={s.attempts[-1].error_kind if s.attempts else '-'}"
                    )
                    continue
                sections.append(
                    f"  - SQL: {s.final_sql}"
                )
                if s.rows:
                    sample = s.rows[:5]
                    sections.append(
                        f"    rows ({len(s.rows)} total, showing up to 5): "
                        f"{json.dumps(sample, default=str)[:600]}"
                    )

        if kg_outs:
            sections.append("Graph outputs:")
            for k in kg_outs:
                sections.append(
                    f"  - kind={k.query_kind} target={k.target} "
                    f"metrics={k.metrics}"
                )
                if k.nodes:
                    sample = [
                        {"id": n.id, "label": n.label,
                         "props": dict(list(n.properties.items())[:5])}
                        for n in k.nodes[:8]
                    ]
                    sections.append(
                        f"    nodes: {json.dumps(sample, default=str)[:600]}"
                    )
                if k.edges:
                    edge_sample = [
                        f"{e.source_id} --{e.rel_type}--> {e.target_id}"
                        for e in k.edges[:8]
                    ]
                    sections.append(
                        "    edges: " + "; ".join(edge_sample)
                    )

        return "\n".join(sections)

    def _call_llm(self, user_payload: str) -> dict:
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=900,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _WRITER_SYSTEM},
                {"role": "user", "content": user_payload[:8000]},
            ],
        )
        raw = (resp.choices[0].message.content or "{}").strip()
        return json.loads(raw)

    @staticmethod
    def _humanize_ids(body: str) -> str:
        """Replace raw `topic_*` / `ent_*` ids with their PG label / name.

        Best-effort: if Postgres is unreachable, return the original body
        untouched. Misses are left as-is (no exceptions raised).
        """
        topic_ids = set(_TOPIC_ID_RE.findall(body))
        entity_ids = set(_ENTITY_ID_RE.findall(body))
        if not (topic_ids or entity_ids):
            return body
        try:
            from services.postgres_service import PostgresService
            pg = PostgresService()
            pg.connect()
        except Exception:
            return body

        replacements: dict[str, str] = {}
        try:
            with pg.cursor() as cur:
                if topic_ids:
                    cur.execute(
                        "SELECT topic_id, label FROM topics_v2 "
                        "WHERE topic_id = ANY(%s)",
                        (list(topic_ids),),
                    )
                    for row in cur.fetchall():
                        label = (row.get("label") or "").strip()
                        if label:
                            replacements[row["topic_id"]] = label
                if entity_ids:
                    cur.execute(
                        "SELECT entity_id, name FROM entities_v2 "
                        "WHERE entity_id = ANY(%s)",
                        (list(entity_ids),),
                    )
                    for row in cur.fetchall():
                        name = (row.get("name") or "").strip()
                        if name:
                            replacements[row["entity_id"]] = name
        except Exception as exc:
            log.warning("report_writer.humanize_ids_error",
                        error=str(exc)[:120])
            return body

        if not replacements:
            return body

        def _sub(match: "re.Match") -> str:
            rid = match.group(0)
            return replacements.get(rid, rid)

        body = _TOPIC_ID_RE.sub(_sub, body)
        body = _ENTITY_ID_RE.sub(_sub, body)
        # Strip cosmetic prefixes like "Topic ID: <label>" -> "<label>"
        body = re.sub(r"\bTopic ID:\s*", "", body)
        body = re.sub(r"\bEntity ID:\s*", "", body)
        return body

    @staticmethod
    def _fallback_body(
        evidence_outs: list[EvidenceOutput],
        sql_outs: list[SQLOutput],
        kg_outs: list[KGOutput],
    ) -> str:
        bits: list[str] = ["I gathered the following data but couldn't compose"
                            " a polished answer:"]
        if evidence_outs:
            n = sum(len(e.bundle.chunks) for e in evidence_outs)
            bits.append(f"- {n} evidence chunks from official sources")
        if sql_outs:
            ok = sum(1 for s in sql_outs if s.success)
            bits.append(f"- {ok} successful SQL queries on community data")
        if kg_outs:
            bits.append(f"- {len(kg_outs)} knowledge-graph results")
        return "\n".join(bits)


def _filter_outputs(execution: PlanExecutionV2, branch: str, model_cls):
    out: list[Any] = []
    for r in execution.results:
        if r.invocation.branch != branch:
            continue
        if not r.status.success or r.output is None:
            continue
        try:
            out.append(model_cls.model_validate(r.output))
        except Exception:
            continue
    return out


def _dedupe_citations(items: list[Citation]) -> list[Citation]:
    seen: set[str] = set()
    out: list[Citation] = []
    for c in items:
        if c.chunk_id in seen:
            continue
        seen.add(c.chunk_id)
        out.append(c)
    return out
