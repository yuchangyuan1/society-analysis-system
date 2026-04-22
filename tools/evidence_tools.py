"""Tools for fetching evidence chunks and official sources at chat time.

These wrap services so capabilities don't need to know how embeddings or
third-party APIs work. Tools are stateless; each call builds a small client
lazily and returns Pydantic outputs.

retrieve_evidence_chunks — semantic search over the internal Chroma articles
collection (the one KnowledgeAgent populates during precompute).
retrieve_official_sources — Wikipedia + NewsSearch fallback; used when a
capability needs authoritative material that may not be in Chroma yet.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from tools.base import ToolInput, ToolOutput, ToolError


# ─── Lazy singletons ─────────────────────────────────────────────────────────

_chroma = None
_embedder = None
_wiki = None
_news = None


def _get_chroma():
    global _chroma
    if _chroma is None:
        from services.chroma_service import ChromaService
        _chroma = ChromaService()
    return _chroma


def _get_embedder():
    global _embedder
    if _embedder is None:
        from services.embeddings_service import EmbeddingsService
        _embedder = EmbeddingsService()
    return _embedder


def _get_wikipedia():
    global _wiki
    if _wiki is None:
        from services.wikipedia_service import WikipediaService
        _wiki = WikipediaService()
    return _wiki


def _get_news():
    global _news
    if _news is None:
        from services.news_search_service import NewsSearchService
        _news = NewsSearchService()
    return _news


# ─── Models ──────────────────────────────────────────────────────────────────

class EvidenceChunk(BaseModel):
    article_id: str
    title: Optional[str] = None
    url: Optional[str] = None
    source_name: Optional[str] = None
    snippet: str = ""
    similarity: float = 0.0


class RetrieveEvidenceChunksInput(ToolInput):
    query_text: str
    n_results: int = 8
    min_similarity: float = 0.0


class RetrieveEvidenceChunksOutput(ToolOutput):
    chunks: list[EvidenceChunk] = Field(default_factory=list)


class OfficialSource(BaseModel):
    article_id: str
    title: str
    url: str
    source_name: str
    snippet: str = ""
    tier: str  # "wikipedia" or "news"


class RetrieveOfficialSourcesInput(ToolInput):
    query_text: str
    include_wikipedia: bool = True
    include_news: bool = True
    news_max_results: int = 3


class RetrieveOfficialSourcesOutput(ToolOutput):
    sources: list[OfficialSource] = Field(default_factory=list)


# ─── Tool functions ──────────────────────────────────────────────────────────

def retrieve_evidence_chunks(
    input: RetrieveEvidenceChunksInput,
) -> RetrieveEvidenceChunksOutput:
    """Semantic search across the internal Chroma articles collection."""
    try:
        embedder = _get_embedder()
        chroma = _get_chroma()
        embedding = embedder.embed(input.query_text)
        hits = chroma.query_articles(embedding, n_results=input.n_results)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"chroma query failed: {exc}") from exc

    from services.chroma_service import ChromaService

    chunks: list[EvidenceChunk] = []
    for hit in hits:
        sim = ChromaService.cosine_similarity(hit.get("distance", 1.0))
        if sim < input.min_similarity:
            continue
        meta = hit.get("metadata") or {}
        chunks.append(
            EvidenceChunk(
                article_id=meta.get("article_id") or hit.get("id", ""),
                title=meta.get("title"),
                url=meta.get("url"),
                source_name=meta.get("source"),
                snippet=(hit.get("document") or "")[:400],
                similarity=round(sim, 4),
            )
        )
    return RetrieveEvidenceChunksOutput(chunks=chunks)


def retrieve_official_sources(
    input: RetrieveOfficialSourcesInput,
) -> RetrieveOfficialSourcesOutput:
    """Best-effort Wikipedia + trusted-news lookup. Network failures are silent."""
    sources: list[OfficialSource] = []

    if input.include_wikipedia:
        try:
            wiki = _get_wikipedia()
            summary = wiki.fetch_summary(input.query_text)
        except Exception:  # noqa: BLE001
            summary = None
        if summary:
            sources.append(
                OfficialSource(
                    article_id=summary.get("article_id", ""),
                    title=summary.get("title", ""),
                    url=summary.get("url", ""),
                    source_name=summary.get("source_name", "Wikipedia"),
                    snippet=summary.get("snippet", ""),
                    tier="wikipedia",
                )
            )

    if input.include_news:
        try:
            news = _get_news()
            articles = news.search_and_fetch(
                input.query_text, max_results=input.news_max_results
            )
        except Exception:  # noqa: BLE001
            articles = []
        for art in articles:
            sources.append(
                OfficialSource(
                    article_id=art.get("article_id", ""),
                    title=art.get("title", ""),
                    url=art.get("url", ""),
                    source_name=art.get("source", ""),
                    snippet=(art.get("body") or "")[:400],
                    tier="news",
                )
            )

    return RetrieveOfficialSourcesOutput(sources=sources)
