"""Embeddings client tests - focus on the deterministic mock.

The OpenAI implementation requires network + a key, so it is exercised
implicitly by the opt-in integration test in ``test_rag_integration.py``.
"""

from __future__ import annotations

import math

from app.llm.embeddings import (
    EMBEDDING_DIM,
    MockEmbeddingsClient,
    get_embeddings_client,
)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def test_mock_embeddings_dim() -> None:
    client = MockEmbeddingsClient()
    [vec] = client.embed(["hello world"])
    assert len(vec) == EMBEDDING_DIM


def test_mock_embeddings_deterministic() -> None:
    """Same input across two clients in two calls must yield identical vectors."""
    a = MockEmbeddingsClient().embed(["UAE passport holders can enter Japan visa-free"])
    b = MockEmbeddingsClient().embed(["UAE passport holders can enter Japan visa-free"])
    assert a == b


def test_mock_embeddings_overlapping_texts_are_similar() -> None:
    """Texts sharing words should have cosine similarity well above 0."""
    client = MockEmbeddingsClient()
    [v1, v2] = client.embed(
        [
            "UAE passport holders can enter Japan visa-free for tourism",
            "Do UAE passport holders need a visa for Japan",
        ]
    )
    sim = _cosine(v1, v2)
    assert sim > 0.4, f"expected meaningful similarity, got {sim}"


def test_mock_embeddings_unrelated_texts_are_dissimilar() -> None:
    """Completely unrelated texts should be near-orthogonal."""
    client = MockEmbeddingsClient()
    [v1, v2] = client.embed(
        ["UAE passport visa Japan tourism rules", "baggage allowance economy class kilograms"]
    )
    sim = _cosine(v1, v2)
    assert sim < 0.2, f"expected low similarity, got {sim}"


def test_mock_embeddings_empty_input() -> None:
    assert MockEmbeddingsClient().embed([]) == []


def test_mock_embeddings_punctuation_is_stripped() -> None:
    """Punctuation around words shouldn't shift them to different dimensions."""
    client = MockEmbeddingsClient()
    [v1, v2] = client.embed(["visa, refund, and baggage", "visa refund and baggage"])
    assert _cosine(v1, v2) > 0.95


def test_factory_returns_mock_for_mock_provider() -> None:
    client = get_embeddings_client(provider="mock")
    assert client.name == "mock"
