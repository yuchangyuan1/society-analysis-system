"""
seed_planner_memory.py - redesign-2026-05 Phase 3.5.

Idempotently load the cold-start ModuleCards and WorkflowExemplars defined
in `services.planner_memory.SEED_*` into Chroma 3 (`chroma_planner`).

Re-run is safe: module_card writes are deterministic by id; workflow_success
seeds may produce up to N (small) extra documents on re-runs but the
conflict-replacement policy (sim >= 0.95 -> LLM-arbitrated overwrite)
keeps Chroma 3 from growing unboundedly.

Usage:
    python -m scripts.seed_planner_memory
    python -m scripts.seed_planner_memory --skip-exemplars
"""
from __future__ import annotations

import argparse
import sys

import structlog

from services.embeddings_service import EmbeddingsService
from services.planner_memory import (
    SEED_MODULE_CARDS,
    SEED_WORKFLOW_EXEMPLARS,
    PlannerMemory,
)

log = structlog.get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed Chroma 3 with module cards and workflow exemplars",
    )
    parser.add_argument("--skip-exemplars", action="store_true",
                        help="Only seed module_card docs.")
    args = parser.parse_args()

    embeddings = EmbeddingsService()
    memory = PlannerMemory()

    for card in SEED_MODULE_CARDS:
        embedding = embeddings.embed(card.doc_text())
        rid = memory.upsert_module_card(card, embedding)
        print(f"module_card:{card.name} -> {rid}")

    if args.skip_exemplars:
        return 0

    for ex in SEED_WORKFLOW_EXEMPLARS:
        embedding = embeddings.embed(ex.doc_text())
        rid = memory.upsert_workflow_success(ex, embedding)
        print(f"workflow_exemplar:{ex.question[:50]}... -> {rid}")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
