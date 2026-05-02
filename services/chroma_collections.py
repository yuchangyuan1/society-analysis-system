"""
Chroma collections facade - redesign-2026-05 Phase 2.

Wraps a single PersistentClient and exposes the three semantic collections
defined in PROJECT_REDESIGN_V2.md section 5b:

    Chroma 1 (`chroma_official`)  - official-source chunks (Evidence Retrieval)
    Chroma 2 (`chroma_nl2sql`)    - schema descriptions + NL2SQL exemplars
    Chroma 3 (`chroma_planner`)   - module cards + planner workflow exemplars

Each collection is exposed as a thin object with `upsert / query / delete /
count`. Heavier behaviour (NL2SQL conflict replacement, planner few-shot
selection) lives in dedicated services on top of this facade.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import chromadb
import structlog
from chromadb.config import Settings

from config import (
    CHROMA_NL2SQL_COLLECTION,
    CHROMA_OFFICIAL_COLLECTION,
    CHROMA_PERSIST_DIR,
    CHROMA_PLANNER_COLLECTION,
)

log = structlog.get_logger(__name__)


@dataclass
class _CollectionWrapper:
    """Minimal Chroma collection adapter; metadata flatten + helpers."""

    name: str
    handle: Any  # chromadb collection

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        if not ids:
            return
        self.handle.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def query(
        self,
        embedding: list[float],
        n_results: int = 8,
        where: Optional[dict] = None,
    ) -> list[dict]:
        results = self.handle.query(
            query_embeddings=[embedding],
            n_results=n_results,
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        return _flatten_query(results)

    def get(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        results = self.handle.get(
            ids=ids, include=["documents", "metadatas"],
        )
        out = []
        for i, rid in enumerate(results.get("ids", []) or []):
            out.append({
                "id": rid,
                "document": (results.get("documents") or [""])[i],
                "metadata": (results.get("metadatas") or [{}])[i],
            })
        return out

    def delete(self, ids: Optional[list[str]] = None,
               where: Optional[dict] = None) -> None:
        if ids:
            self.handle.delete(ids=ids)
        elif where:
            self.handle.delete(where=where)

    def count(self, where: Optional[dict] = None) -> int:
        # Chroma .count() doesn't accept where; emulate via .get
        if where is None:
            return self.handle.count()
        results = self.handle.get(where=where, include=[])
        return len(results.get("ids") or [])


class ChromaCollections:
    """Singleton-friendly wrapper around the three v2 collections."""

    def __init__(self, persist_dir: str = CHROMA_PERSIST_DIR) -> None:
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self.official = _CollectionWrapper(
            name=CHROMA_OFFICIAL_COLLECTION,
            handle=self._client.get_or_create_collection(
                CHROMA_OFFICIAL_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            ),
        )
        self.nl2sql = _CollectionWrapper(
            name=CHROMA_NL2SQL_COLLECTION,
            handle=self._client.get_or_create_collection(
                CHROMA_NL2SQL_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            ),
        )
        self.planner = _CollectionWrapper(
            name=CHROMA_PLANNER_COLLECTION,
            handle=self._client.get_or_create_collection(
                CHROMA_PLANNER_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            ),
        )
        log.info("chroma_collections.ready",
                 persist_dir=persist_dir,
                 official=self.official.count(),
                 nl2sql=self.nl2sql.count(),
                 planner=self.planner.count())


def _flatten_query(results: dict) -> list[dict]:
    out = []
    ids = (results.get("ids") or [[]])[0]
    docs = (results.get("documents") or [[]])[0]
    metas = (results.get("metadatas") or [[]])[0]
    dists = (results.get("distances") or [[]])[0]
    for i, rid in enumerate(ids):
        out.append({
            "id": rid,
            "document": docs[i] if i < len(docs) else "",
            "metadata": metas[i] if i < len(metas) else {},
            "distance": dists[i] if i < len(dists) else 1.0,
            "similarity": 1.0 - (dists[i] if i < len(dists) else 1.0),
        })
    return out
