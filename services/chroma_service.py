"""
Chroma vector store — semantic retrieval for posts, claims, articles.
"""
from __future__ import annotations

from typing import Optional
import structlog
import chromadb
from chromadb.config import Settings

from config import (
    CHROMA_PERSIST_DIR,
    CHROMA_POSTS_COLLECTION,
    CHROMA_CLAIMS_COLLECTION,
    CHROMA_ARTICLES_COLLECTION,
)

log = structlog.get_logger(__name__)


class ChromaService:
    def __init__(self, persist_dir: str = CHROMA_PERSIST_DIR) -> None:
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._posts = self._client.get_or_create_collection(
            CHROMA_POSTS_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        self._claims = self._client.get_or_create_collection(
            CHROMA_CLAIMS_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        self._articles = self._client.get_or_create_collection(
            CHROMA_ARTICLES_COLLECTION, metadata={"hnsw:space": "cosine"}
        )
        log.info("chroma.initialized", persist_dir=persist_dir)

    # ── Posts ──────────────────────────────────────────────────────────────────

    def upsert_post(self, post_id: str, embedding: list[float],
                    text: str, metadata: Optional[dict] = None) -> None:
        self._posts.upsert(
            ids=[post_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata or {"source": "post"}],
        )

    def query_posts(self, embedding: list[float], n_results: int = 10) -> list[dict]:
        results = self._posts.query(
            query_embeddings=[embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        return self._flatten(results)

    # ── Claims ─────────────────────────────────────────────────────────────────

    def upsert_claim(self, claim_id: str, embedding: list[float],
                     text: str, metadata: Optional[dict] = None) -> None:
        self._claims.upsert(
            ids=[claim_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata or {"source": "claim"}],
        )

    def query_claims(self, embedding: list[float],
                     n_results: int = 10) -> list[dict]:
        """Returns list of {id, document, metadata, distance}."""
        results = self._claims.query(
            query_embeddings=[embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        return self._flatten(results)

    # ── Articles ───────────────────────────────────────────────────────────────

    def upsert_article(self, article_id: str, chunk_id: str,
                       embedding: list[float], text: str,
                       metadata: Optional[dict] = None) -> None:
        self._articles.upsert(
            ids=[f"{article_id}::{chunk_id}"],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{**(metadata or {}), "article_id": article_id}],
        )

    def query_articles(self, embedding: list[float],
                       n_results: int = 10) -> list[dict]:
        results = self._articles.query(
            query_embeddings=[embedding],
            n_results=n_results,
            include=["documents", "metadatas", "distances"],
        )
        return self._flatten(results)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _flatten(results: dict) -> list[dict]:
        out = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        for i, rid in enumerate(ids):
            out.append({
                "id": rid,
                "document": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "distance": dists[i] if i < len(dists) else 1.0,
            })
        return out

    @staticmethod
    def cosine_similarity(distance: float) -> float:
        """Chroma returns cosine distance (1 - similarity); convert."""
        return 1.0 - distance
