"""
Schema double-write helper - redesign-2026-05 Phase 2 5b.

Encapsulates the contract between Schema-aware Agent, Postgres `schema_meta`,
and Chroma 2 (`chroma_nl2sql`, kind=schema). Two responsibilities:

1. `apply_proposal()` - atomic-ish double-write of a SchemaProposal:
       - opens a Postgres transaction, upserts schema_meta rows
       - upserts Chroma 2 schema docs in a staging set
       - on Postgres commit, swaps the staging docs into place by deleting
         orphans (kind=schema docs whose fingerprint did not appear in this
         proposal)

2. `verify()` - returns a `ConsistencyReport` comparing
       PG information_schema  vs  schema_meta  vs  Chroma 2 schema docs.
       Used by `tests/test_schema_consistency.py` and the rebuild CLI.

Chroma has no native transactions; staging-swap = "write new ids first, then
delete obsolete ones" so a failure in the middle leaves the previous schema
docs intact.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Optional

from models.schema_proposal import ColumnSpec, SchemaProposal
from services.chroma_collections import ChromaCollections
from services.embeddings_service import EmbeddingsService
from services.nl2sql_memory import NL2SQLMemory
from services.postgres_service import PostgresService

log = structlog.get_logger(__name__)


# Internal PG columns that should NOT be exposed to NL2SQL.
# `text_tsv` is a derived index, `ingested_at` is an audit timestamp.
# We intentionally hide them from schema_meta + Chroma 2 so the NL2SQL
# branch never queries them.
_INTERNAL_COLUMNS: set[str] = {"posts_v2.text_tsv", "posts_v2.ingested_at"}


@dataclass
class ConsistencyReport:
    pg_columns: set[str] = field(default_factory=set)            # f"{table}.{col}"
    schema_meta_columns: set[str] = field(default_factory=set)
    chroma_schema_columns: set[str] = field(default_factory=set)
    extra_columns: set[str] = field(default_factory=set)         # location='extra'

    fingerprint_pg: dict[str, str] = field(default_factory=dict)
    fingerprint_chroma: dict[str, str] = field(default_factory=dict)

    @property
    def missing_in_chroma(self) -> set[str]:
        return self.schema_meta_columns - self.chroma_schema_columns

    @property
    def orphan_in_chroma(self) -> set[str]:
        return self.chroma_schema_columns - self.schema_meta_columns

    @property
    def missing_in_schema_meta(self) -> set[str]:
        # PG columns marked internal are intentionally hidden.
        # JSONB-extra columns aren't physical PG columns, so they are
        # never expected to appear in this set anyway.
        return (self.pg_columns - self.schema_meta_columns) - _INTERNAL_COLUMNS

    @property
    def fingerprint_drift(self) -> set[str]:
        # Extra (JSONB) columns have no PG fingerprint to compare against.
        keys = (set(self.fingerprint_pg) & set(self.fingerprint_chroma)) - self.extra_columns
        return {k for k in keys
                if self.fingerprint_pg[k] != self.fingerprint_chroma[k]}

    def is_consistent(self) -> bool:
        return (
            not self.missing_in_chroma
            and not self.orphan_in_chroma
            and not self.missing_in_schema_meta
            and not self.fingerprint_drift
        )

    def to_dict(self) -> dict:
        return {
            "is_consistent": self.is_consistent(),
            "missing_in_chroma": sorted(self.missing_in_chroma),
            "orphan_in_chroma": sorted(self.orphan_in_chroma),
            "missing_in_schema_meta": sorted(self.missing_in_schema_meta),
            "fingerprint_drift": sorted(self.fingerprint_drift),
            "extra_columns_known": sorted(self.extra_columns),
        }


class SchemaSync:
    """High-level facade for the double-write + verify cycle."""

    def __init__(
        self,
        pg: Optional[PostgresService] = None,
        memory: Optional[NL2SQLMemory] = None,
        embeddings: Optional[EmbeddingsService] = None,
    ) -> None:
        self._pg = pg or PostgresService()
        self._memory = memory or NL2SQLMemory()
        self._embeddings = embeddings or EmbeddingsService()

    # ── Apply ──────────────────────────────────────────────────────────────────

    def apply_proposal(self, proposal: SchemaProposal) -> None:
        """Double-write a SchemaProposal to PG schema_meta + Chroma 2."""
        kept_chroma_ids: set[str] = set()
        # 1. Postgres upsert (transactional via cursor() context)
        for col in proposal.columns:
            self._pg.upsert_schema_meta(
                table_name=col.table_name,
                column_name=col.column_name,
                column_type=col.column_type,
                description=col.description,
                sample_values=col.sample_values,
                fingerprint=col.fingerprint(),
                in_extra=(col.location == "extra"),
            )

        # 2. Chroma 2 staging upsert (one doc per column)
        for col in proposal.columns:
            doc_text = self._format_schema_doc(col)
            embedding = self._embeddings.embed(doc_text)
            record_id = self._memory.upsert_schema(
                table_name=col.table_name,
                column_name=col.column_name,
                text=doc_text,
                embedding=embedding,
                fingerprint=col.fingerprint(),
                sample_values=col.sample_values,
            )
            kept_chroma_ids.add(record_id)

        # 3. Drop orphans (kind=schema docs that survived from a stale proposal)
        existing = self._memory._cols.nl2sql.handle.get(  # type: ignore[attr-defined]
            where={"kind": "schema"}, include=[],
        )
        existing_ids = list(existing.get("ids") or [])
        orphans = [rid for rid in existing_ids if rid not in kept_chroma_ids]
        if orphans:
            self._memory._cols.nl2sql.delete(ids=orphans)
            log.info("schema_sync.orphans_deleted", count=len(orphans))

        log.info("schema_sync.applied",
                 run_id=proposal.run_id,
                 columns=len(proposal.columns),
                 fingerprint=proposal.schema_fingerprint()[:12],
                 kept=len(kept_chroma_ids),
                 orphans=len(orphans))

    # ── Verify ─────────────────────────────────────────────────────────────────

    def verify(self, table_name: str = "posts_v2") -> ConsistencyReport:
        report = ConsistencyReport()

        # PG live information_schema (filter to the requested table; skip
        # tsvector/audit cols are handled by `_INTERNAL_COLUMNS` later).
        pg_cols = self._pg.list_information_schema_columns(table_name)
        for c in pg_cols:
            key = f"{table_name}.{c['column_name']}"
            report.pg_columns.add(key)

        # PG schema_meta rows for THIS table only (we may have docs for
        # topics_v2 / entities_v2 / post_entities_v2 in the same table; they
        # are not part of this verify call).
        meta_rows = self._pg.list_schema_meta(table_name)
        for row in meta_rows:
            key = f"{row['table_name']}.{row['column_name']}"
            report.schema_meta_columns.add(key)
            report.fingerprint_pg[key] = row["fingerprint"]
            if row.get("in_extra"):
                report.extra_columns.add(key)

        # Chroma 2 schema docs scoped to the same table
        existing = self._memory._cols.nl2sql.handle.get(  # type: ignore[attr-defined]
            where={"$and": [
                {"kind": "schema"},
                {"table_name": table_name},
            ]},
            include=["metadatas"],
        )
        for meta in (existing.get("metadatas") or []):
            if not isinstance(meta, dict):
                continue
            key = f"{meta.get('table_name')}.{meta.get('column_name')}"
            if "None.None" in key:
                continue
            report.chroma_schema_columns.add(key)
            report.fingerprint_chroma[key] = meta.get("fingerprint", "")

        return report

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _format_schema_doc(col: ColumnSpec) -> str:
        body = (
            f"Table: {col.table_name}\n"
            f"Column: {col.column_name}\n"
            f"Type: {col.column_type}\n"
            f"Location: {col.location}\n"
            f"Description: {col.description}"
        )
        if col.sample_values:
            body += "\nExamples: " + ", ".join(col.sample_values[:5])
        return body
