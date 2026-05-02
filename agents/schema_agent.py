"""
Schema-aware Agent - redesign-2026-05 Phase 2 5b.

Inspects a sample of incoming posts, emits a `SchemaProposal` describing:
  - the fixed core columns of `posts_v2` (never modified)
  - which dynamic fields should be folded into the `extra` JSONB column

Hard rules (PROJECT_REDESIGN_V2.md):
- NEVER issue ALTER TABLE. Core columns stay fixed; dynamic ones go to extra.
- After each run, the agent writes:
    * one row per column to Postgres `schema_meta`
    * one document per column to Chroma 2 (kind=schema), with the same
      fingerprint, inside a transaction-like staging swap.
- `tests/test_schema_consistency.py` checks that PG `information_schema`
  matches the schema_meta + Chroma 2 fingerprints. Drift triggers a
  `scripts/rebuild_chroma2_schema.py` run.

The LLM is only asked to *describe* and *summarise* fields; never to invent
new tables. The system prompt is intentionally narrow.
"""
from __future__ import annotations

import json
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.post import Post
from models.schema_proposal import ColumnSpec, SchemaProposal

log = structlog.get_logger(__name__)


# ── Fixed core columns (never inferred by the LLM) ────────────────────────────

_CORE_POSTS_V2_COLUMNS: list[ColumnSpec] = [
    ColumnSpec(table_name="posts_v2", column_name="post_id", column_type="TEXT",
               description="Unique identifier for the post.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="account_id",
               column_type="TEXT",
               description="Stable account identifier of the author.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="author", column_type="TEXT",
               description="Human-readable author handle (e.g. Reddit username).",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="text", column_type="TEXT",
               description="Compressed/merged post text (includes folded image OCR).",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="posted_at",
               column_type="TIMESTAMPTZ",
               description="When the post was originally published.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="subreddit",
               column_type="TEXT",
               description="Subreddit / channel name; nullable for non-Reddit sources.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="source", column_type="TEXT",
               description="Origin platform: reddit | telegram | x | fixture.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="topic_id", column_type="TEXT",
               description="Topic id assigned by post-level clustering.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="dominant_emotion",
               column_type="TEXT",
               description="Primary emotion: fear | anger | hope | disgust | neutral.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="emotion_score",
               column_type="REAL",
               description="Intensity of the dominant emotion in [0, 1].",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="like_count",
               column_type="INTEGER",
               description="Number of likes / upvotes the post received.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="reply_count",
               column_type="INTEGER",
               description="Number of replies / comments to the post.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="retweet_count",
               column_type="INTEGER",
               description="Number of retweets / shares of the post.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="simhash",
               column_type="BIGINT",
               description="64-bit simhash for near-duplicate detection.",
               location="core"),
    ColumnSpec(table_name="posts_v2", column_name="extra", column_type="JSONB",
               description="Dynamic fields proposed by Schema-aware Agent.",
               location="core"),
    # ── topics_v2 (referenced via posts_v2.topic_id) ────────────────────────
    ColumnSpec(table_name="topics_v2", column_name="topic_id", column_type="TEXT",
               description="Primary key for a topic; equals posts_v2.topic_id "
                           "for posts in the topic.",
               location="core"),
    ColumnSpec(table_name="topics_v2", column_name="label", column_type="TEXT",
               description="Human-readable topic label, e.g. 'Media Trust "
                           "and Vaccine Information'. JOIN posts_v2 "
                           "ON posts_v2.topic_id = topics_v2.topic_id and "
                           "filter via WHERE topics_v2.label = 'X' (or "
                           "LOWER(label) LIKE '%x%' for fuzzy match) when "
                           "the user names a topic. NEVER pass the label "
                           "string to plainto_tsquery -- text_tsv indexes "
                           "post BODY, not topic names.",
               location="core"),
    ColumnSpec(table_name="topics_v2", column_name="post_count",
               column_type="INTEGER",
               description="Number of posts assigned to this topic.",
               location="core"),
    ColumnSpec(table_name="topics_v2", column_name="dominant_emotion",
               column_type="TEXT",
               description="Most common emotion across the topic's posts.",
               location="core"),
    ColumnSpec(table_name="topics_v2", column_name="centroid_text",
               column_type="TEXT",
               description="Representative post text for the topic cluster.",
               location="core"),
    # ── entities_v2 + post_entities_v2 (named-entity index) ─────────────────
    ColumnSpec(table_name="entities_v2", column_name="entity_id",
               column_type="TEXT",
               description="Primary key for a named entity (person / org / "
                           "place / event).",
               location="core"),
    ColumnSpec(table_name="entities_v2", column_name="name", column_type="TEXT",
               description="Canonical entity name, e.g. 'World Health Organization'.",
               location="core"),
    ColumnSpec(table_name="entities_v2", column_name="entity_type",
               column_type="TEXT",
               description="One of PERSON | ORG | LOC | EVENT | OTHER.",
               location="core"),
    ColumnSpec(table_name="entities_v2", column_name="mention_count",
               column_type="INTEGER",
               description="Cumulative number of mentions across all posts.",
               location="core"),
    ColumnSpec(table_name="post_entities_v2", column_name="post_id",
               column_type="TEXT",
               description="Foreign key to posts_v2.post_id.",
               location="core"),
    ColumnSpec(table_name="post_entities_v2", column_name="entity_id",
               column_type="TEXT",
               description="Foreign key to entities_v2.entity_id. JOIN to "
                           "find which entities a post mentions.",
               location="core"),
    ColumnSpec(table_name="post_entities_v2", column_name="confidence",
               column_type="REAL",
               description="Extractor confidence in [0, 1].",
               location="core"),
]


_PROPOSAL_SYSTEM = """You are a database schema curator for a social-media analysis system.
You inspect a sample of posts and produce JSON describing the dynamic fields that
should be stored in the `extra` JSONB column on `posts_v2`.

Other tables already exist in the schema (do NOT duplicate fields they own):
- topics_v2(topic_id, label, post_count, dominant_emotion, centroid_text)
- entities_v2(entity_id, name, entity_type, mention_count)
- post_entities_v2(post_id, entity_id, confidence)

Rules:
- NEVER propose new core columns. Only describe fields that go into `extra`.
- Each field needs: name (snake_case), type (string|int|float|bool|array), description.
- Skip fields that are already mapped to core columns (post_id, account_id,
  author, text, posted_at, subreddit, source, topic_id, dominant_emotion,
  emotion_score, like_count, reply_count, retweet_count, simhash).
- Skip fields that duplicate other tables. Entities live in entities_v2 +
  post_entities_v2 (do NOT propose `mentioned_sources` or `entities` on
  posts_v2).
- Description must be a single sentence in plain English, suitable for an
  NL2SQL agent to read.
- Return at most 8 fields.
- Output STRICT JSON: {"fields": [{"name": "...", "type": "...", "description": "..."}]}
"""


class SchemaAgent:
    """LLM-backed proposer of `extra` field shapes."""

    def __init__(
        self,
        client: Optional[openai.OpenAI] = None,
        model: str = OPENAI_MODEL,
        sample_size: int = 12,
    ) -> None:
        self._client = client or openai.OpenAI(api_key=OPENAI_API_KEY)
        self._model = model
        self._sample_size = sample_size

    # ── Public ─────────────────────────────────────────────────────────────────

    def propose(self, run_id: str, posts: list[Post]) -> SchemaProposal:
        """Build a SchemaProposal from a representative slice of posts."""
        proposal = SchemaProposal(run_id=run_id, columns=list(_CORE_POSTS_V2_COLUMNS))

        if not posts:
            log.warning("schema_agent.no_posts", run_id=run_id)
            return proposal

        sample = posts[: self._sample_size]
        try:
            extra_specs = self._llm_propose_extra(sample)
        except Exception as exc:
            log.error("schema_agent.llm_error", run_id=run_id, error=str(exc)[:160])
            extra_specs = []

        for spec in extra_specs:
            proposal.columns.append(spec)

        log.info("schema_agent.proposed",
                 run_id=run_id,
                 core=len(proposal.core_columns()),
                 extra=len(proposal.extra_columns()),
                 fingerprint=proposal.schema_fingerprint()[:12])
        return proposal

    # ── Internal ───────────────────────────────────────────────────────────────

    def _llm_propose_extra(self, sample: list[Post]) -> list[ColumnSpec]:
        snippets = []
        for i, p in enumerate(sample, start=1):
            snippets.append(
                f"{i}. id={p.id} likes={p.like_count} replies={p.reply_count} "
                f"emotion={p.emotion or '-'} text={p.text[:200]!r}"
            )
        user_msg = "Sample posts (one per line):\n" + "\n".join(snippets)

        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=512,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _PROPOSAL_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        )
        raw = (resp.choices[0].message.content or "{}").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("schema_agent.json_parse_error", raw=raw[:200])
            return []

        # Build sample_values map per field name (best-effort lookup in extra)
        specs: list[ColumnSpec] = []
        seen: set[str] = set()
        type_map = {
            "string": "TEXT", "str": "TEXT", "text": "TEXT",
            "int": "INTEGER", "integer": "INTEGER",
            "float": "REAL", "number": "REAL",
            "bool": "BOOLEAN", "boolean": "BOOLEAN",
            "array": "JSONB", "list": "JSONB",
            "object": "JSONB", "dict": "JSONB",
        }
        # Names already in core or owned by other v2 tables. Even if the LLM
        # proposes them, we drop them so PG / Chroma 2 don't accumulate
        # duplicate descriptions.
        deny = {
            "post_id", "account_id", "author", "text", "posted_at",
            "subreddit", "source", "topic_id", "dominant_emotion",
            "emotion_score", "like_count", "reply_count", "retweet_count",
            "simhash", "extra",
            # owned by entities_v2 / post_entities_v2
            "entities", "mentioned_entities", "mentioned_sources",
            # owned by topics_v2
            "topic_label", "topic",
            # legacy v1 names sometimes regenerated by the LLM
            "likes", "replies", "retweets", "channel_name",
        }
        for field in data.get("fields") or []:
            name = (field.get("name") or "").strip()
            type_raw = (field.get("type") or "").lower().strip()
            description = (field.get("description") or "").strip()
            if not name or not description:
                continue
            if name in deny:
                log.info("schema_agent.proposal_filtered", name=name)
                continue
            if name in seen:
                continue
            seen.add(name)
            sql_type = type_map.get(type_raw, "TEXT")
            samples = self._collect_samples(sample, name)
            specs.append(ColumnSpec(
                table_name="posts_v2",
                column_name=name,
                column_type=sql_type,
                description=description,
                sample_values=samples[:5],
                location="extra",
            ))
        return specs[:8]

    @staticmethod
    def _collect_samples(posts: list[Post], field: str) -> list[str]:
        out: list[str] = []
        for p in posts:
            value = None
            if hasattr(p, field):
                value = getattr(p, field)
            else:
                # Best-effort lookup; v1 Post doesn't have a real `extra` dict
                # but downstream pipelines may attach one.
                extra = getattr(p, "extra", None)
                if isinstance(extra, dict):
                    value = extra.get(field)
            if value in (None, "", [], {}):
                continue
            out.append(str(value)[:80])
        return out
