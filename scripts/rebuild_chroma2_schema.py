"""
rebuild_chroma2_schema.py - redesign-2026-05 Phase 2 5b.

Rebuild the schema-description portion of Chroma 2 (`chroma_nl2sql`,
kind=schema) from Postgres `information_schema` + `schema_meta`.

Triggers (PROJECT_REDESIGN_V2.md Phase 2):
- Consistency test failure (`tests/test_schema_consistency.py`)
- Chroma 2 corruption / disk loss
- Schema-aware Agent historical drift
- Manual migration

Modes:
    --dry-run         Print what would change; no writes.
    --keep-experience (default) Only rebuild kind=schema docs; leave
                      success/error documents untouched.
    --full-reset      Wipe ALL Chroma 2 documents then rebuild schema only.
                      Use with care: erases NL2SQL exemplars and error lessons.

Usage:
    python -m scripts.rebuild_chroma2_schema --dry-run
    python -m scripts.rebuild_chroma2_schema --keep-experience
    python -m scripts.rebuild_chroma2_schema --full-reset --yes
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

import structlog

from models.schema_proposal import ColumnSpec, SchemaProposal
from services.embeddings_service import EmbeddingsService
from services.nl2sql_memory import NL2SQLMemory
from services.postgres_service import PostgresService
from services.schema_sync import SchemaSync

log = structlog.get_logger(__name__)


def build_proposal_from_pg(
    pg: PostgresService, table_name: str = "posts_v2",
) -> SchemaProposal:
    """Reconstruct a SchemaProposal from current PG state.

    Sources, in priority order:
      1. `schema_meta` rows for the table (description, sample_values)
      2. `information_schema.columns` (column_name, type) - any column not in
         schema_meta gets a stub description.
    """
    proposal = SchemaProposal(run_id="rebuild")
    meta_rows = {r["column_name"]: r for r in pg.list_schema_meta(table_name)}
    pg_cols = pg.list_information_schema_columns(table_name)

    for c in pg_cols:
        col_name = c["column_name"]
        col_type = c["data_type"].upper()
        meta = meta_rows.get(col_name)
        description = (meta.get("description") if meta
                       else f"Auto-recovered description for {col_name}")
        try:
            sample_values = json.loads(meta["sample_values"]) if meta else []
        except (TypeError, json.JSONDecodeError):
            sample_values = []
        location = "extra" if (meta and meta.get("in_extra")) else "core"
        proposal.columns.append(ColumnSpec(
            table_name=table_name,
            column_name=col_name,
            column_type=col_type,
            description=description,
            sample_values=sample_values,
            location=location,
        ))
    return proposal


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild Chroma 2 schema docs from Postgres",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing.")
    parser.add_argument("--keep-experience", action="store_true",
                        help="(default) Only rebuild kind=schema docs.")
    parser.add_argument("--full-reset", action="store_true",
                        help="Wipe ALL Chroma 2 docs then rebuild schema.")
    parser.add_argument("--table", default="posts_v2",
                        help="Table name to rebuild (default: posts_v2).")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation for destructive operations.")
    args = parser.parse_args()

    if args.full_reset and args.keep_experience:
        parser.error("--full-reset and --keep-experience are mutually exclusive.")

    pg = PostgresService()
    pg.connect()
    sync = SchemaSync(pg=pg)

    proposal = build_proposal_from_pg(pg, table_name=args.table)
    print(f"Reconstructed proposal: {len(proposal.columns)} columns")
    print(f"Aggregate fingerprint: {proposal.schema_fingerprint()[:16]}...")

    report_before = sync.verify(table_name=args.table)
    print("\nBefore:")
    print(json.dumps(report_before.to_dict(), indent=2))

    if args.dry_run:
        print("\n[dry-run] No writes. Exiting.")
        return 0

    if args.full_reset:
        if not args.yes:
            try:
                confirm = input(
                    "Full reset will DELETE all Chroma 2 documents "
                    "(schema + success + error). Continue? [yes/N] "
                )
            except EOFError:
                confirm = ""
            if confirm.strip().lower() != "yes":
                print("Aborted.")
                return 1
        # Wipe all docs - delete by getting all ids
        cols = sync._memory._cols  # type: ignore[attr-defined]
        existing = cols.nl2sql.handle.get(include=[])
        ids = list(existing.get("ids") or [])
        if ids:
            cols.nl2sql.delete(ids=ids)
        log.info("rebuild.full_reset_done", deleted=len(ids))

    # Apply (this writes new docs and clears orphans of the same kind=schema)
    sync.apply_proposal(proposal)
    report_after = sync.verify(table_name=args.table)
    print("\nAfter:")
    print(json.dumps(report_after.to_dict(), indent=2))

    if not report_after.is_consistent():
        print("\nWARNING: consistency report still has differences.")
        return 2
    print("\nOK: schema is consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
