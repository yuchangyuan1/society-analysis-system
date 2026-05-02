"""
decay_chroma_experience.py - redesign-2026-05 Phase 5.1.

Sweeps Chroma 2 (`chroma_nl2sql`) and Chroma 3 (`chroma_planner`) and:
- Drops records older than EXPERIENCE_TTL_DAYS (default 30) when their
  metadata.kind is in DECAY_KINDS.
- Drops records whose confidence is below EXPERIENCE_MIN_CONFIDENCE.
- Leaves kind=schema and kind=module_card untouched (they are anchor
  documents, not learned experience).

Designed to run as a daily cron job:
    python -m scripts.decay_chroma_experience
    python -m scripts.decay_chroma_experience --dry-run
    python -m scripts.decay_chroma_experience --collection nl2sql
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

import structlog

from config import EXPERIENCE_MIN_CONFIDENCE, EXPERIENCE_TTL_DAYS
from services.chroma_collections import ChromaCollections

log = structlog.get_logger(__name__)


# Kinds that are anchor docs and must not be decayed.
ANCHOR_KINDS = {"schema", "module_card"}
# Kinds that ARE eligible for TTL + confidence-based decay.
DECAY_KINDS = {
    "success", "error",                       # Chroma 2
    "workflow_success", "workflow_error",     # Chroma 3
    "composition_error",                      # Chroma 3
}


def sweep_collection(
    name: str,
    handle,                       # _CollectionWrapper
    *,
    ttl_days: int,
    min_confidence: float,
    dry_run: bool,
) -> dict:
    """Walk the collection, identify decayed records, optionally delete."""
    cutoff_ts = time.time() - ttl_days * 24 * 3600
    raw = handle.handle.get(include=["metadatas"])
    ids = list(raw.get("ids") or [])
    metas = list(raw.get("metadatas") or [])

    to_drop: list[str] = []
    breakdown = {"ttl": 0, "low_confidence": 0, "anchor_skipped": 0,
                 "no_kind": 0, "kept": 0}
    for rid, meta in zip(ids, metas):
        if not isinstance(meta, dict):
            breakdown["no_kind"] += 1
            continue
        kind = meta.get("kind") or ""
        if kind in ANCHOR_KINDS:
            breakdown["anchor_skipped"] += 1
            continue
        if kind not in DECAY_KINDS:
            breakdown["no_kind"] += 1
            continue
        last_used = float(meta.get("last_used_at", 0) or 0)
        confidence = float(meta.get("confidence", 0.5) or 0.0)
        reason = None
        if last_used and last_used < cutoff_ts:
            reason = "ttl"
        elif confidence < min_confidence:
            reason = "low_confidence"
        if reason:
            to_drop.append(rid)
            breakdown[reason] += 1
        else:
            breakdown["kept"] += 1

    if to_drop and not dry_run:
        handle.delete(ids=to_drop)
        log.info("decay.deleted", collection=name, count=len(to_drop))

    return {
        "collection": name,
        "total_records": len(ids),
        "to_drop": len(to_drop),
        "deleted": 0 if dry_run else len(to_drop),
        "breakdown": breakdown,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decay-sweep Chroma 2 / Chroma 3 experience records",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be deleted without writing.")
    parser.add_argument("--collection", choices=["nl2sql", "planner", "all"],
                        default="all",
                        help="Limit the sweep to one collection.")
    parser.add_argument("--ttl-days", type=int, default=EXPERIENCE_TTL_DAYS,
                        help="Records last used > this many days ago are decayed.")
    parser.add_argument("--min-confidence", type=float,
                        default=EXPERIENCE_MIN_CONFIDENCE,
                        help="Records below this confidence are decayed.")
    args = parser.parse_args()

    cols = ChromaCollections()
    targets: list[tuple[str, object]] = []
    if args.collection in ("nl2sql", "all"):
        targets.append(("nl2sql", cols.nl2sql))
    if args.collection in ("planner", "all"):
        targets.append(("planner", cols.planner))

    overall = []
    for name, handle in targets:
        result = sweep_collection(
            name, handle,
            ttl_days=args.ttl_days,
            min_confidence=args.min_confidence,
            dry_run=args.dry_run,
        )
        overall.append(result)
        print(
            f"{name}: total={result['total_records']} "
            f"to_drop={result['to_drop']} "
            f"deleted={result['deleted']} "
            f"breakdown={result['breakdown']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
