"""
Planner memory service - redesign-2026-05 Phase 3.5.

Wraps Chroma 3 (`chroma_planner`). Stores three flavours of documents:

    kind=module_card         - one per branch (evidence / nl2sql / kg)
    kind=workflow_success    - successful (question, branches_used) exemplars
    kind=workflow_error      - planner-attribution failures (Critic-driven)
    kind=composition_error   - report-writer failures (citation/numeric)

Conflict policy mirrors NL2SQL memory (PROJECT_REDESIGN_V2.md 7c-H, Q11=B):
    sim < 0.92                -> append
    0.92 <= sim < 0.95        -> direct overwrite
    sim >= 0.95               -> LLM-arbitrated comparison
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Literal, Optional

import structlog

from config import (
    NL2SQL_CONFLICT_SIM_HIGH,
    NL2SQL_CONFLICT_SIM_LOW,
)
from models.module_card import ModuleCard, WorkflowExemplar
from services.chroma_collections import ChromaCollections

log = structlog.get_logger(__name__)


PlannerKind = Literal[
    "module_card",
    "workflow_success",
    "workflow_error",
    "composition_error",
]


@dataclass
class PlannerMemory:
    collections: Optional[ChromaCollections] = None
    sim_low: float = NL2SQL_CONFLICT_SIM_LOW
    sim_high: float = NL2SQL_CONFLICT_SIM_HIGH
    llm_judge: Optional[Callable[[str, str], bool]] = None

    def __post_init__(self) -> None:
        self.collections = self.collections or ChromaCollections()
        self.llm_judge = self.llm_judge or (lambda new, old: True)

    # ── Module cards ──────────────────────────────────────────────────────────

    def upsert_module_card(self, card: ModuleCard, embedding: list[float]) -> str:
        record_id = f"module_card::{card.name}"
        meta = {
            "kind": "module_card",
            "branch": card.name,
            "updated_at": time.time(),
        }
        self.collections.planner.upsert(
            ids=[record_id],
            embeddings=[embedding],
            documents=[card.doc_text()],
            metadatas=[meta],
        )
        return record_id

    def upsert_workflow_success(
        self, exemplar: WorkflowExemplar, embedding: list[float],
    ) -> str:
        meta = {
            "kind": "workflow_success",
            "branches": ",".join(exemplar.branches_used),
            "confidence": 0.5,
            "hit_count": 0,
            "last_used_at": time.time(),
        }
        return self._upsert_with_conflict(exemplar.doc_text(), embedding, meta)

    def upsert_workflow_error(
        self,
        question: str,
        branches_used: list[str],
        error_kind: str,
        embedding: list[float],
    ) -> str:
        text = (f"Question: {question}\n"
                f"Branches: {', '.join(branches_used)}\n"
                f"Error: {error_kind}")
        meta = {
            "kind": "workflow_error",
            "error_kind": error_kind,
            "branches": ",".join(branches_used),
            "confidence": 0.5,
            "hit_count": 0,
            "last_used_at": time.time(),
        }
        return self._upsert_with_conflict(text, embedding, meta)

    def upsert_composition_error(
        self,
        question: str,
        error_kind: str,
        excerpt: str,
        embedding: list[float],
    ) -> str:
        text = (f"Question: {question}\nError: {error_kind}\n"
                f"Excerpt: {excerpt[:300]}")
        meta = {
            "kind": "composition_error",
            "error_kind": error_kind,
            "confidence": 0.5,
            "hit_count": 0,
            "last_used_at": time.time(),
        }
        return self._upsert_with_conflict(text, embedding, meta)

    # ── Recall ───────────────────────────────────────────────────────────────

    def recall_module_cards(
        self, embedding: list[float], n_results: int = 3,
    ) -> list[dict]:
        return self.collections.planner.query(
            embedding=embedding, n_results=n_results,
            where={"kind": "module_card"},
        )

    def recall_workflow_exemplars(
        self, embedding: list[float], n_results: int = 5,
    ) -> list[dict]:
        return self.collections.planner.query(
            embedding=embedding, n_results=n_results,
            where={"kind": "workflow_success"},
        )

    def recall_workflow_errors(
        self, embedding: list[float], n_results: int = 3,
    ) -> list[dict]:
        return self.collections.planner.query(
            embedding=embedding, n_results=n_results,
            where={"kind": "workflow_error"},
        )

    def count_branch_combo_successes(self, branches_used: list[str]) -> int:
        """Used by Q9 confidence rule (PROJECT_REDESIGN_V2.md 7c-B)."""
        key = ",".join(branches_used)
        return self.collections.planner.count(where={
            "kind": "workflow_success",
            "branches": key,
        })

    def delete_records(self, record_ids: list[str]) -> None:
        if record_ids:
            self.collections.planner.delete(ids=record_ids)

    # ── Internals ────────────────────────────────────────────────────────────

    def _upsert_with_conflict(
        self, text: str, embedding: list[float], metadata: dict,
    ) -> str:
        kind = metadata.get("kind", "")
        existing = self.collections.planner.query(
            embedding=embedding, n_results=5, where={"kind": kind},
        )
        if existing:
            top = existing[0]
            sim = float(top.get("similarity", 0.0))
            old_id = top["id"]
            old_text = top.get("document", "")
            if sim >= self.sim_high:
                if self.llm_judge(text, old_text):
                    self.collections.planner.delete(ids=[old_id])
            elif sim >= self.sim_low:
                self.collections.planner.delete(ids=[old_id])
        record_id = f"{kind}::{uuid.uuid4().hex}"
        self.collections.planner.upsert(
            ids=[record_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata],
        )
        return record_id


# ── Seed cards (PROJECT_REDESIGN_V2.md Phase 2 5b cold-start) ────────────────

SEED_MODULE_CARDS: list[ModuleCard] = [
    ModuleCard(
        name="evidence",
        description=("Hybrid (dense + BM25 + RRF + rerank) retrieval over "
                      "Chroma 1, the official-sources collection."),
        when_to_use=[
            "User asks for fact-check evidence from authoritative outlets.",
            "User wants to compare community claims against official reporting.",
            "Question explicitly mentions sources like BBC / NYT / Reuters / Wikipedia.",
        ],
        when_not_to_use=[
            "Pure structural questions about who posted / how often.",
            "Questions about graph relationships between accounts.",
        ],
        input_schema={
            "query": "string",
            "metadata_filter": "dict (optional, e.g. {'tier': 'reputable_media'})",
        },
        output_schema={"bundle": "EvidenceBundle (chunks + citations + ranks)"},
        examples=[
            {"question": "Did the BBC report on the vaccine recall last week?"},
            {"question": "What is the WHO's official position on long COVID?"},
        ],
    ),
    ModuleCard(
        name="nl2sql",
        description=("Generates and executes a safe read-only Postgres "
                      "SELECT against posts_v2 / topics_v2 / entities_v2."),
        when_to_use=[
            "Counting / filtering / grouping over community posts.",
            "Topic-scoped questions ('show me posts in topic T').",
            "Time-window queries on posted_at.",
        ],
        when_not_to_use=[
            "Questions about graph paths or centrality - use the kg branch.",
            "Open-ended fact-check questions - use the evidence branch.",
        ],
        input_schema={"nl_query": "string"},
        output_schema={"sql_output": "SQLOutput (final_sql, rows, attempts)"},
        examples=[
            {"question": "How many posts in topic T have dominant_emotion='anger'?"},
            {"question": "List the top 10 authors in the last 7 days."},
        ],
    ),
    ModuleCard(
        name="kg",
        description=(
            "The ONLY branch that can do multi-hop reply traversal, "
            "centrality (PageRank / betweenness), and community detection "
            "(Louvain) over the Kuzu graph. SQL cannot express any of "
            "these - routing them to nl2sql produces shallow GROUP BY "
            "answers that miss the structure of the spread."
        ),
        when_to_use=[
            "Tracing a reply chain between two accounts (propagation_trace).",
            "Identifying influence by PageRank, not raw post counts "
            "(influencer_query).",
            "Detecting coordinated posting / bot networks "
            "(coordination_check, Louvain communities).",
            "Diagnosing echo chambers / polarised clusters "
            "(community_structure, modularity).",
            "Surfacing viral cascades / longest reply threads "
            "(cascade_query).",
            "Answering 'who is amplifying' / 'how did this spread' / "
            "'are they organised'.",
        ],
        when_not_to_use=[
            "Simple counts or filters that don't need graph structure - "
            "use nl2sql.",
            "Verifying facts against external authoritative reports - "
            "use evidence.",
            "Listing the text of posts in a topic - nl2sql is faster.",
        ],
        input_schema={
            "query_kind": (
                "propagation_path | key_nodes | topic_correlation | "
                "cascade_tree | viral_cascade | influencer_rank | "
                "bridge_accounts | coordinated_groups | echo_chamber"
            ),
            "target": (
                "dict (topic_id / source_account / target_account / "
                "root_post_id / top_k / min_size)"
            ),
        },
        output_schema={"kg_output": "KGOutput (nodes, edges, metrics)"},
        examples=[
            {"question": "Show me how the vaccine rumour spread from "
                          "alice to dave."},
            {"question": "Who's the most influential account in the "
                          "climate topic? (rank by PageRank, not post count)"},
            {"question": "Is the surge of anti-vaccine posts coming from "
                          "a coordinated group?"},
            {"question": "Is topic T an echo chamber?"},
            {"question": "What's the deepest reply thread under any "
                          "post in this topic?"},
            {"question": "Find the bridge accounts that connect the "
                          "vaccine cluster and the climate cluster."},
        ],
    ),
]


SEED_WORKFLOW_EXEMPLARS: list[WorkflowExemplar] = [
    WorkflowExemplar(
        question="Is the BBC story about the vaccine recall being amplified on Reddit?",
        branches_used=["evidence", "nl2sql", "kg"],
        rationale="Need official source (evidence), Reddit volume (nl2sql), "
                  "amplifier accounts (kg).",
    ),
    WorkflowExemplar(
        question="What topics are trending right now?",
        branches_used=["nl2sql", "kg"],
        rationale="Trending = volume by topic (nl2sql) + the amplifier "
                  "structure behind each topic (kg).",
    ),
    WorkflowExemplar(
        question="Who is the most influential account in topic T?",
        branches_used=["kg", "nl2sql"],
        rationale="Centrality from graph + post counts as a tiebreaker.",
    ),
    WorkflowExemplar(
        question="What did Reuters say about the WHO statement, and how is the community reacting?",
        branches_used=["evidence", "nl2sql"],
        rationale="Authoritative recap plus community-side commentary.",
    ),
    WorkflowExemplar(
        question="How many angry posts about climate in the past week?",
        branches_used=["nl2sql"],
        rationale="Pure single-aggregation SQL job; no other branch helps.",
    ),
    WorkflowExemplar(
        question="Compare the official line and community sentiment on the vaccine recall.",
        branches_used=["evidence", "nl2sql", "kg"],
        rationale="Canonical 3-source comparison: official + volume + propagation.",
    ),
    WorkflowExemplar(
        question="Are accounts in topic T connected to topic U through shared entities?",
        branches_used=["kg", "nl2sql"],
        rationale="Cross-topic graph correlation, with NL2SQL providing "
                  "topic-level context (post counts, dominant emotion).",
    ),
    WorkflowExemplar(
        question="What's the dominant narrative in the current trending topic?",
        branches_used=["nl2sql", "kg", "evidence"],
        rationale="Topic content (nl2sql) + amplifiers (kg) + whether "
                  "official sources back it up (evidence).",
    ),
    # ── Phase C: KG-specialised exemplars ────────────────────────────────────
    WorkflowExemplar(
        question="Trace how the rumour spread from alice to dave through replies.",
        branches_used=["kg"],
        rationale="propagation_trace: multi-hop reply chain - "
                  "SQL cannot express it.",
    ),
    WorkflowExemplar(
        question="Who is most influential in the vaccine topic?",
        branches_used=["kg", "nl2sql"],
        rationale="influencer_query: PageRank from KG (real influence), "
                  "post counts from nl2sql for context.",
    ),
    WorkflowExemplar(
        question="Is this surge of posts coming from a coordinated group?",
        branches_used=["kg"],
        rationale="coordination_check: Louvain communities; SQL self-joins "
                  "cannot do modularity optimisation.",
    ),
    WorkflowExemplar(
        question="Is topic T an echo chamber?",
        branches_used=["kg", "nl2sql"],
        rationale="community_structure: KG modularity score + nl2sql for "
                  "the within-cluster post sample.",
    ),
    WorkflowExemplar(
        question="Show me the longest viral cascade in the climate topic.",
        branches_used=["kg"],
        rationale="cascade_query: viral_cascade ranks reply-tree depth; "
                  "SQL cannot follow recursive replies.",
    ),
]
