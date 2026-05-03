"""
Pipeline transactions / commit_state tests - production hardening Day 4.

Verifies:
  - new_run() writes commit_state="pending"
  - finalize() writes commit_state="committed"
  - mark_failed() writes commit_state="failed"
  - list_pending_runs() finds pending + failed, ignores committed
  - PostgresService.delete_run_data hits all v2 tables filtered by run_id
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from models.manifest import RunManifest
from services.manifest_service import ManifestService


def test_new_run_writes_pending(tmp_path: Path):
    ms = ManifestService(runs_root=tmp_path)
    m = ms.new_run(query_text="hello")
    assert m.commit_state == "pending"
    on_disk = (tmp_path / m.run_id / "run_manifest.json").read_text(encoding="utf-8")
    assert '"commit_state": "pending"' in on_disk


def test_finalize_writes_committed(tmp_path: Path):
    ms = ManifestService(runs_root=tmp_path)
    m = ms.new_run(query_text="hello")
    ms.finalize(m, post_count=42)
    on_disk = RunManifest.model_validate_json(
        (tmp_path / m.run_id / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert on_disk.commit_state == "committed"
    assert on_disk.post_count == 42
    assert on_disk.finished_at is not None


def test_mark_failed_writes_failed(tmp_path: Path):
    ms = ManifestService(runs_root=tmp_path)
    m = ms.new_run(query_text="hello")
    ms.mark_failed(m, error="OOM")
    on_disk = RunManifest.model_validate_json(
        (tmp_path / m.run_id / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert on_disk.commit_state == "failed"
    assert on_disk.finished_at is not None


def test_list_pending_skips_committed(tmp_path: Path):
    ms = ManifestService(runs_root=tmp_path)
    m1 = ms.new_run(query_text="run-1")
    m2 = ms.new_run(query_text="run-2")
    m3 = ms.new_run(query_text="run-3")
    ms.finalize(m1)            # committed
    ms.mark_failed(m2)         # failed
    # m3 stays pending

    pending = ms.list_pending_runs()
    states = {p.run_id: p.commit_state for p in pending}
    assert m1.run_id not in states
    assert states[m2.run_id] == "failed"
    assert states[m3.run_id] == "pending"


def test_delete_run_data_calls_all_tables():
    """PostgresService.delete_run_data hits all 4 v2 tables in dependency order."""
    from services.postgres_service import PostgresService

    pg = PostgresService.__new__(PostgresService)
    pg._conn = None
    cursor = MagicMock()
    cursor.rowcount = 7

    # Mock cursor() context manager
    class _CursorCtx:
        def __enter__(self_): return cursor
        def __exit__(self_, *a): return False

    pg.cursor = lambda: _CursorCtx()
    out = pg.delete_run_data("run-x")
    assert set(out.keys()) == {
        "post_entities_v2", "posts_v2", "topics_v2", "entities_v2",
    }
    # 4 deletes executed
    assert cursor.execute.call_count == 4
    # First DELETE must be post_entities_v2 (FK child)
    first_sql = cursor.execute.call_args_list[0].args[0]
    assert "post_entities_v2" in first_sql
