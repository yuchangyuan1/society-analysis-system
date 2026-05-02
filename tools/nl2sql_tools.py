"""
NL2SQL tools - redesign-2026-05 Phase 3.3.

Branch B (NL2SQL). Three-step loop with internal repair (PROJECT_REDESIGN_V2.md
7b-(3) layered error handling):

    generate_sql()         - LLM produces a SELECT, fed by:
                                * Chroma 2 schema docs (kind=schema)
                                * Chroma 2 success exemplars (kind=success)
                                * Chroma 2 error lessons (kind=error)
    execute_and_validate() - Read-only DSN, SELECT-only whitelist,
                             LIMIT enforcement, statement_timeout.
    repair_sql()           - On error or unexpected empty rows, retry up
                             to NL2SQL_MAX_REPAIR_ROUNDS with the failure
                             folded into the prompt.

Errors that ARE Critic's concern (sql_empty_result with the wrong intent
match) bubble up via SQLOutput.success=False. Errors handled internally
(syntax / unknown column / timeout) only go to the repair loop and never
to Critic.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import openai
import psycopg2
import psycopg2.extras
import structlog

from config import (
    NL2SQL_MAX_REPAIR_ROUNDS,
    NL2SQL_RESULT_ROW_LIMIT,
    NL2SQL_STATEMENT_TIMEOUT_MS,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    POSTGRES_DSN,
    POSTGRES_READONLY_DSN,
)
from models.branch_output import SQLAttempt, SQLOutput
from services.embeddings_service import EmbeddingsService
from services.nl2sql_memory import NL2SQLMemory

log = structlog.get_logger(__name__)


# ── Whitelist + safety helpers ───────────────────────────────────────────────

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|"
    r"COPY|VACUUM|ANALYZE|REINDEX|CLUSTER|REFRESH|EXECUTE|CALL|DO)\b",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)


def _sanitise_sql(sql: str, row_limit: int) -> tuple[str, Optional[str]]:
    """Return (safe_sql, error_kind). Reject anything that isn't pure SELECT."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        return "", "sql_syntax"
    if ";" in text:
        return "", "sql_syntax"  # disallow multi-statement
    if not re.match(r"^\s*(WITH|SELECT)\b", text, re.IGNORECASE):
        return "", "sql_syntax"
    if _FORBIDDEN_KEYWORDS.search(text):
        return "", "sql_syntax"
    # Force a LIMIT
    m = _LIMIT_RE.search(text)
    if m:
        try:
            current = int(m.group(1))
            if current > row_limit:
                text = _LIMIT_RE.sub(f"LIMIT {row_limit}", text, count=1)
        except ValueError:
            return "", "sql_syntax"
    else:
        text = f"{text} LIMIT {row_limit}"
    return text, None


# ── Generation prompt ────────────────────────────────────────────────────────

_GENERATE_SYSTEM = """You write SAFE Postgres SELECT queries.
You will be given:
  - the user question
  - relevant column descriptions (schema hints)
  - a few correct (NL, SQL) examples
  - error lessons to avoid

Rules:
- Output ONLY one SELECT query (or a CTE WITH ... SELECT). No semicolons.
- Use only the tables and columns explicitly listed in schema hints.
- For free-text matching on posts.text use the existing tsvector via
  `text_tsv @@ plainto_tsquery('english', '...')`. Avoid LIKE on huge text.
- Always add a LIMIT (the runtime will cap it).
- Prefer joining via posts_v2.topic_id when the question is topic-scoped.
- NEVER return raw machine IDs (topic_id, entity_id, post_id, account_id)
  as the only user-facing column. Always JOIN to a human-readable name:
    * topic_id    -> JOIN topics_v2 ON ... SELECT topics_v2.label
    * entity_id   -> JOIN entities_v2 ON ... SELECT entities_v2.name
    * account_id  -> SELECT posts_v2.author (already human-readable)
  IDs are fine alongside the readable name (e.g. SELECT label, topic_id),
  but never alone unless the user explicitly asked "what is the id of ...".
- "What topics are trending" -> SELECT label, post_count FROM topics_v2
  ORDER BY post_count DESC.

Topic filtering rules (read carefully -- this is where most NL2SQL errors come from):
- If the user prompt provides "Pre-resolved topic_ids", USE THEM as the
  filter and IGNORE label matching entirely:
      WHERE posts_v2.topic_id IN ('topic_xxx', 'topic_yyy')
  These ids were chosen by an embedding-based semantic match, so they
  already cover the user's topic phrase even if the label text differs.
- Otherwise, when the user names a topic by exact label, JOIN on topic_id:
      SELECT posts_v2.text, posts_v2.author, posts_v2.dominant_emotion
      FROM posts_v2
      JOIN topics_v2 ON posts_v2.topic_id = topics_v2.topic_id
      WHERE topics_v2.label = 'X'
- Fuzzy fallback (only when no pre-resolved ids and no exact label):
      WHERE LOWER(topics_v2.label) LIKE LOWER('%X%')
- text_tsv is for matching POST CONTENT keywords (e.g. "posts mentioning
  vaccines"), NOT for matching topic labels. Topic labels are short
  curated phrases that virtually never appear verbatim in post text.
- When the user asks "what was discussed", project the actual post
  content (posts_v2.text and friends), not just the topic label that
  they already gave you.

Respond as JSON: {"sql": "..."}.
"""


# ── Service ──────────────────────────────────────────────────────────────────

@dataclass
class NL2SQLTool:
    memory: Optional[NL2SQLMemory] = None
    embeddings: Optional[EmbeddingsService] = None
    client: Optional[openai.OpenAI] = None
    model: str = OPENAI_MODEL
    row_limit: int = NL2SQL_RESULT_ROW_LIMIT
    statement_timeout_ms: int = NL2SQL_STATEMENT_TIMEOUT_MS
    max_repair_rounds: int = NL2SQL_MAX_REPAIR_ROUNDS
    readonly_dsn: Optional[str] = None

    def __post_init__(self) -> None:
        self.memory = self.memory or NL2SQLMemory()
        self.embeddings = self.embeddings or EmbeddingsService()
        self.client = self.client or openai.OpenAI(api_key=OPENAI_API_KEY)
        self.readonly_dsn = (self.readonly_dsn or POSTGRES_READONLY_DSN
                             or POSTGRES_DSN)

    # ── Public ─────────────────────────────────────────────────────────────────

    def answer(
        self,
        nl_query: str,
        topic_id_hints: Optional[list[dict]] = None,
    ) -> SQLOutput:
        t0 = time.monotonic()
        out = SQLOutput(nl_query=nl_query)

        if not nl_query or not nl_query.strip():
            out.attempts.append(SQLAttempt(sql="", error="empty_query",
                                            error_kind="sql_syntax"))
            out.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return out

        # 1. Recall context from Chroma 2
        embedding = self.embeddings.embed(nl_query)
        schema_hits = self.memory.recall_schema(embedding, n_results=8)
        success_hits = self.memory.recall_success(embedding, n_results=5)
        error_hits = self.memory.recall_errors(embedding, n_results=3)

        out.used_schema_hints = [h["id"] for h in schema_hits]
        out.used_examples = [h["id"] for h in success_hits]
        out.used_error_lessons = [h["id"] for h in error_hits]

        prompt_user = self._build_user_prompt(
            nl_query, schema_hits, success_hits, error_hits,
            topic_id_hints=topic_id_hints,
        )

        # 2. Generate + execute + repair loop
        rounds = 0
        last_error: Optional[str] = None
        last_kind: Optional[str] = None

        while rounds <= self.max_repair_rounds:
            sql_raw = self._llm_generate(prompt_user, last_error, last_kind)
            safe_sql, syntax_kind = _sanitise_sql(sql_raw, self.row_limit)
            if syntax_kind is not None:
                attempt = SQLAttempt(sql=sql_raw or "",
                                      error="sanitiser rejected",
                                      error_kind=syntax_kind)
                out.attempts.append(attempt)
                last_error = "sanitiser rejected the SQL"
                last_kind = syntax_kind
                rounds += 1
                continue

            attempt = SQLAttempt(sql=safe_sql)
            try:
                rows, columns = self._execute(safe_sql)
                attempt.rows_returned = len(rows)
                out.attempts.append(attempt)
                out.final_sql = safe_sql
                out.rows = rows
                out.columns = columns
                out.success = True
                # Success-loop validation (PROJECT_REDESIGN_V2.md 7b-(3)):
                if attempt.rows_returned == 0:
                    # Soft signal: empty result. Do not retry; surface
                    # error_kind so Critic can decide.
                    attempt.error_kind = "sql_empty_result"
                if attempt.rows_returned >= self.row_limit:
                    # Hit cap; flag a soft warning but treat as success.
                    attempt.error_kind = "sql_limit_hit"
                break
            except _SQLExecutionError as exc:
                attempt.error = exc.detail[:240]
                attempt.error_kind = exc.kind
                out.attempts.append(attempt)
                last_error = exc.detail[:240]
                last_kind = exc.kind
                rounds += 1
                # Persist error lesson on third (final) failure
                if rounds > self.max_repair_rounds:
                    self._record_error_lesson(nl_query, safe_sql, exc, embedding)
                continue

        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.info("nl2sql.answer_done",
                 nl=nl_query[:60],
                 success=out.success,
                 attempts=len(out.attempts),
                 rows=len(out.rows),
                 elapsed_ms=out.elapsed_ms)
        return out

    def record_success(self, nl_query: str, sql: str) -> str:
        """External callers (Critic/Reflection) record a clean success."""
        embedding = self.embeddings.embed(nl_query)
        return self.memory.upsert_success(nl_query, sql, embedding)

    # ── Internals ──────────────────────────────────────────────────────────────

    def _build_user_prompt(
        self, nl_query: str,
        schema_hits: list[dict],
        success_hits: list[dict],
        error_hits: list[dict],
        topic_id_hints: Optional[list[dict]] = None,
    ) -> str:
        sections: list[str] = [f"Question: {nl_query}"]
        if topic_id_hints:
            # Tell the LLM exactly which topic_ids semantically match.
            # The system prompt says: prefer WHERE topic_id IN (...) over
            # label LIKE matching when these are provided.
            hint_lines = [
                f"  - {h['topic_id']!r} (label={h.get('label','')!r}, "
                f"similarity={h.get('similarity', 0):.2f})"
                for h in topic_id_hints
            ]
            sections.append(
                "Pre-resolved topic_ids (use these instead of label matching):"
            )
            sections.extend(hint_lines)
        if schema_hits:
            sections.append("Schema hints:")
            for h in schema_hits:
                sections.append(f"  - {h.get('document', '')}")
        if success_hits:
            sections.append("Successful examples:")
            for h in success_hits:
                sections.append(f"  - {h.get('document', '')}")
        if error_hits:
            sections.append("Avoid these mistakes:")
            for h in error_hits:
                sections.append(f"  - {h.get('document', '')}")
        return "\n".join(sections)

    def _llm_generate(
        self,
        user_prompt: str,
        prev_error: Optional[str],
        prev_kind: Optional[str],
    ) -> str:
        if prev_error:
            user_prompt = (
                f"{user_prompt}\n\nPrevious attempt failed (kind={prev_kind}): "
                f"{prev_error}\nWrite a corrected query."
            )
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=512,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _GENERATE_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw = (resp.choices[0].message.content or "{}").strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return ""
            return (data.get("sql") or "").strip()
        except Exception as exc:
            log.error("nl2sql.llm_error", error=str(exc)[:120])
            return ""

    def _execute(self, sql: str) -> tuple[list[dict], list[str]]:
        try:
            conn = psycopg2.connect(
                self.readonly_dsn,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
        except Exception as exc:
            raise _SQLExecutionError("sql_connection", str(exc))
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(f"SET LOCAL statement_timeout = {self.statement_timeout_ms}")
                    cur.execute("SET TRANSACTION READ ONLY")
                    try:
                        cur.execute(sql)
                    except psycopg2.errors.UndefinedColumn as exc:
                        raise _SQLExecutionError("sql_unknown_column", str(exc))
                    except psycopg2.errors.UndefinedTable as exc:
                        raise _SQLExecutionError("sql_unknown_column", str(exc))
                    except psycopg2.errors.SyntaxError as exc:
                        raise _SQLExecutionError("sql_syntax", str(exc))
                    except psycopg2.errors.QueryCanceled as exc:
                        raise _SQLExecutionError("sql_timeout", str(exc))
                    except psycopg2.errors.DataError as exc:
                        raise _SQLExecutionError("sql_type_mismatch", str(exc))
                    except psycopg2.Error as exc:
                        raise _SQLExecutionError("sql_other", str(exc))
                    rows = list(cur.fetchall())
                    columns = ([d[0] for d in cur.description]
                               if cur.description else [])
                    return rows, columns
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _record_error_lesson(
        self,
        nl_query: str,
        sql: str,
        exc: "_SQLExecutionError",
        embedding: list[float],
    ) -> None:
        """Push a curated error pattern into Chroma 2 (kind=error)."""
        try:
            bad_pattern = sql or "(no SQL produced)"
            self.memory.upsert_error(
                failure_reason=f"{exc.kind}: {exc.detail[:200]}",
                bad_pattern=f"NL: {nl_query}\nSQL: {bad_pattern}",
                embedding=embedding,
            )
        except Exception as record_exc:
            log.error("nl2sql.error_record_failed",
                      error=str(record_exc)[:120])


@dataclass
class _SQLExecutionError(Exception):
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail[:200]}"
