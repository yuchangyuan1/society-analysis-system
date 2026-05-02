"""
Schema double-write consistency tests - redesign-2026-05 Phase 2 5b.

Verifies that PG `information_schema` + `schema_meta` + Chroma 2 (kind=schema)
all agree. The three required checks (PROJECT_REDESIGN_V2.md):

1. test_pg_chroma2_fingerprint_match
   -> Every column in PG `information_schema.columns` has a matching
      schema_meta row with the same fingerprint, and the Chroma 2 doc
      carries that fingerprint too.

2. test_every_pg_column_has_chroma_doc
   -> No PG column lacks a Chroma 2 schema doc.

3. test_no_orphan_chroma_docs
   -> Every Chroma 2 schema doc points to a real PG column.

The tests use mocked Postgres + Chroma layers so they run in CI without
real services. A second integration version (skip-marked) hits live
services when `PYTEST_RUN_LIVE_SCHEMA=1` is set.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from models.schema_proposal import ColumnSpec, SchemaProposal
from services.schema_sync import ConsistencyReport, SchemaSync


def _build_proposal() -> SchemaProposal:
    proposal = SchemaProposal(run_id="test")
    proposal.columns = [
        ColumnSpec(table_name="posts_v2", column_name="post_id",
                   column_type="TEXT",
                   description="Unique post id.", location="core"),
        ColumnSpec(table_name="posts_v2", column_name="text",
                   column_type="TEXT",
                   description="Post text.", location="core"),
        ColumnSpec(table_name="posts_v2", column_name="vote_ratio",
                   column_type="REAL",
                   description="Upvote ratio in 0..1.", location="extra"),
    ]
    return proposal


def _make_consistent_pg() -> MagicMock:
    pg = MagicMock()
    pg.list_information_schema_columns.return_value = [
        {"column_name": "post_id", "data_type": "TEXT"},
        {"column_name": "text", "data_type": "TEXT"},
        {"column_name": "vote_ratio", "data_type": "REAL"},
    ]
    pg.list_schema_meta.return_value = [
        {"table_name": "posts_v2", "column_name": "post_id",
         "fingerprint": "fp_post_id"},
        {"table_name": "posts_v2", "column_name": "text",
         "fingerprint": "fp_text"},
        {"table_name": "posts_v2", "column_name": "vote_ratio",
         "fingerprint": "fp_vote_ratio"},
    ]
    return pg


def _make_chroma_with(metadata_rows: list[dict]) -> MagicMock:
    """Build a minimal NL2SQLMemory mock that exposes a `_cols.nl2sql.handle.get`."""
    handle = MagicMock()
    handle.get.return_value = {"metadatas": metadata_rows, "ids": [
        f"id_{i}" for i in range(len(metadata_rows))
    ]}
    nl2sql = MagicMock()
    nl2sql.handle = handle
    cols = MagicMock()
    cols.nl2sql = nl2sql
    memory = MagicMock()
    memory._cols = cols
    return memory


def test_pg_chroma2_fingerprint_match():
    pg = _make_consistent_pg()
    chroma_metas = [
        {"table_name": "posts_v2", "column_name": "post_id",
         "fingerprint": "fp_post_id"},
        {"table_name": "posts_v2", "column_name": "text",
         "fingerprint": "fp_text"},
        {"table_name": "posts_v2", "column_name": "vote_ratio",
         "fingerprint": "fp_vote_ratio"},
    ]
    memory = _make_chroma_with(chroma_metas)
    sync = SchemaSync(pg=pg, memory=memory, embeddings=MagicMock())

    report = sync.verify(table_name="posts_v2")
    assert report.is_consistent(), report.to_dict()
    assert report.fingerprint_drift == set()


def test_every_pg_column_has_chroma_doc():
    pg = _make_consistent_pg()
    # Chroma is missing vote_ratio
    chroma_metas = [
        {"table_name": "posts_v2", "column_name": "post_id",
         "fingerprint": "fp_post_id"},
        {"table_name": "posts_v2", "column_name": "text",
         "fingerprint": "fp_text"},
    ]
    memory = _make_chroma_with(chroma_metas)
    sync = SchemaSync(pg=pg, memory=memory, embeddings=MagicMock())

    report = sync.verify(table_name="posts_v2")
    assert "posts_v2.vote_ratio" in report.missing_in_chroma
    assert not report.is_consistent()


def test_no_orphan_chroma_docs():
    pg = _make_consistent_pg()
    # Chroma has a stale doc pointing to a removed column
    chroma_metas = [
        {"table_name": "posts_v2", "column_name": "post_id",
         "fingerprint": "fp_post_id"},
        {"table_name": "posts_v2", "column_name": "text",
         "fingerprint": "fp_text"},
        {"table_name": "posts_v2", "column_name": "vote_ratio",
         "fingerprint": "fp_vote_ratio"},
        {"table_name": "posts_v2", "column_name": "removed_col",
         "fingerprint": "fp_removed"},
    ]
    memory = _make_chroma_with(chroma_metas)
    sync = SchemaSync(pg=pg, memory=memory, embeddings=MagicMock())

    report = sync.verify(table_name="posts_v2")
    assert "posts_v2.removed_col" in report.orphan_in_chroma
    assert not report.is_consistent()


def test_fingerprint_drift_detected():
    pg = _make_consistent_pg()
    chroma_metas = [
        {"table_name": "posts_v2", "column_name": "post_id",
         "fingerprint": "fp_post_id"},
        {"table_name": "posts_v2", "column_name": "text",
         "fingerprint": "MISMATCH"},
        {"table_name": "posts_v2", "column_name": "vote_ratio",
         "fingerprint": "fp_vote_ratio"},
    ]
    memory = _make_chroma_with(chroma_metas)
    sync = SchemaSync(pg=pg, memory=memory, embeddings=MagicMock())

    report = sync.verify(table_name="posts_v2")
    assert "posts_v2.text" in report.fingerprint_drift
    assert not report.is_consistent()


def test_consistency_report_to_dict():
    report = ConsistencyReport()
    report.pg_columns = {"posts_v2.a", "posts_v2.b"}
    report.schema_meta_columns = {"posts_v2.a", "posts_v2.b"}
    report.chroma_schema_columns = {"posts_v2.a", "posts_v2.b"}
    report.fingerprint_pg = {"posts_v2.a": "x", "posts_v2.b": "y"}
    report.fingerprint_chroma = {"posts_v2.a": "x", "posts_v2.b": "y"}
    d = report.to_dict()
    assert d["is_consistent"] is True


@pytest.mark.skipif(
    os.getenv("PYTEST_RUN_LIVE_SCHEMA") != "1",
    reason="Set PYTEST_RUN_LIVE_SCHEMA=1 to run against real PG + Chroma",
)
def test_live_schema_consistency():
    """Integration variant: hits real PG + Chroma. Skipped by default."""
    sync = SchemaSync()
    report = sync.verify(table_name="posts_v2")
    assert report.is_consistent(), report.to_dict()
