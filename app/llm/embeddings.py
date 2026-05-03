"""Embeddings layer.

Provider-agnostic, mirroring the LLM client design. Two implementations:

* :class:`OpenAIEmbeddingsClient` - production. Uses ``text-embedding-3-small``
  (1536 dims, ~$0.02 per million tokens - costs cents to embed our entire KB).
* :class:`MockEmbeddingsClient` - deterministic hash-based bag-of-words for
  unit tests and offline demos. Same input → same vector, and texts sharing
  words have non-zero cosine similarity, so retrieval threshold tests work
  without making API calls.

Why a separate file instead of bundling into the LLM client: embeddings
have a different request shape (batch in, vectors out) and different cost
profile. Keeping them separate stops the LLM client growing into a god-object.
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

from app.config import get_settings

EMBEDDING_DIM = 1536  # matches text-embedding-3-small


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingsClient(Protocol):
    """Contract every embeddings backend must satisfy."""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one float vector per input text. All vectors have ``dim`` elements."""
        ...


# ---------------------------------------------------------------------------
# OpenAI implementation
# ---------------------------------------------------------------------------


class OpenAIEmbeddingsClient:
    """Production embeddings via OpenAI."""

    name = "openai"
    dim = EMBEDDING_DIM

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        from openai import OpenAI

        settings = get_settings()
        key = api_key or settings.openai_api_key
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY missing. Set it in .env or use MockEmbeddingsClient."
            )
        self._client = OpenAI(api_key=key)
        self._model = model or settings.embeddings_model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(model=self._model, input=texts)
        return [d.embedding for d in response.data]


# ---------------------------------------------------------------------------
# Mock implementation - deterministic hash-based BoW
# ---------------------------------------------------------------------------


class MockEmbeddingsClient:
    """Deterministic hash-based bag-of-words.

    Each word in the input maps to a single dimension via MD5; the vector
    counts word occurrences and L2-normalizes. Two texts sharing words
    have positive cosine similarity; unrelated texts are near-orthogonal.

    Reproducible across runs (uses ``hashlib``, not Python's salted ``hash()``).
    Used by unit tests and offline demos.
    """

    name = "mock"
    dim = EMBEDDING_DIM

    @staticmethod
    def _word_to_dim(word: str) -> int:
        digest = hashlib.md5(word.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "little") % EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            v = [0.0] * EMBEDDING_DIM
            # Lowercase + simple word split. Good enough - tests don't need stemming.
            for word in text.lower().split():
                stripped = word.strip(".,!?;:()[]{}\"'-")
                if not stripped:
                    continue
                v[self._word_to_dim(stripped)] += 1.0
            norm = math.sqrt(sum(x * x for x in v))
            if norm > 0:
                v = [x / norm for x in v]
            vectors.append(v)
        return vectors


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_embeddings_client(provider: str | None = None) -> EmbeddingsClient:
    """Return the configured embeddings backend.

    Falls back to :class:`MockEmbeddingsClient` with a printed warning if
    OpenAI cannot be instantiated. This keeps tests and demos runnable
    without a live key.
    """
    settings = get_settings()
    chosen = (provider or settings.llm_provider).lower()

    if chosen == "mock":
        return MockEmbeddingsClient()
    if chosen == "openai":
        try:
            return OpenAIEmbeddingsClient()
        except (RuntimeError, ImportError) as e:
            print(f"[embeddings] OpenAI unavailable ({e}); falling back to MockEmbeddingsClient.")
            return MockEmbeddingsClient()
    if chosen == "anthropic":
        # Anthropic has no first-party embeddings API; use OpenAI for now.
        try:
            return OpenAIEmbeddingsClient()
        except (RuntimeError, ImportError):
            return MockEmbeddingsClient()
    raise ValueError(f"Unknown LLM_PROVIDER for embeddings: {chosen!r}")


__all__ = [
    "EMBEDDING_DIM",
    "EmbeddingsClient",
    "MockEmbeddingsClient",
    "OpenAIEmbeddingsClient",
    "get_embeddings_client",
]
