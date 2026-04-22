"""
Embeddings service — wraps OpenAI text-embedding-3-small.
Used for cross-modal text-image retrieval and claim deduplication.
"""
from __future__ import annotations

import structlog
from openai import OpenAI

from config import OPENAI_API_KEY, EMBEDDING_MODEL, EMBEDDING_DIM

log = structlog.get_logger(__name__)


class EmbeddingsService:
    def __init__(self) -> None:
        self._client = OpenAI(api_key=OPENAI_API_KEY)
        self._model = EMBEDDING_MODEL

    def embed(self, text: str) -> list[float]:
        """Embed a single text string. Returns a 1536-dim vector."""
        text = text.replace("\n", " ").strip()
        if not text:
            return [0.0] * EMBEDDING_DIM
        try:
            response = self._client.embeddings.create(
                input=[text],
                model=self._model,
            )
            return response.data[0].embedding
        except Exception as exc:
            log.error("embeddings.error", text=text[:80], error=str(exc))
            return [0.0] * EMBEDDING_DIM

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in one API call (up to 2048 inputs)."""
        cleaned = [t.replace("\n", " ").strip() for t in texts]
        # Replace empty strings with a placeholder to avoid API errors
        safe = [t if t else "." for t in cleaned]
        try:
            response = self._client.embeddings.create(
                input=safe,
                model=self._model,
            )
            vecs = [item.embedding for item in response.data]
            # Zero-out embeddings for originally empty strings
            for i, t in enumerate(cleaned):
                if not t:
                    vecs[i] = [0.0] * EMBEDDING_DIM
            return vecs
        except Exception as exc:
            log.error("embeddings.batch_error", count=len(texts), error=str(exc))
            return [[0.0] * EMBEDDING_DIM for _ in texts]
