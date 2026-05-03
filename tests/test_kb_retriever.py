"""KB retriever tests — chunking, indexing, threshold gate, cache reuse."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.llm.embeddings import MockEmbeddingsClient
from app.tools.kb_retriever import (
    KBRetriever,
    chunk_markdown,
    kb_content_hash,
    load_kb_chunks,
)

# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


def test_chunker_splits_by_h2_sections() -> None:
    md = """# Title

intro paragraph

## Section A

Body of A.

## Section B

Body of B.
"""
    chunks = chunk_markdown(md, doc="x.md")
    assert len(chunks) == 2
    assert chunks[0].section == "Section A"
    assert chunks[1].section == "Section B"
    assert "Body of A" in chunks[0].content
    assert "Body of B" in chunks[1].content
    # Content includes the heading so heading-style queries still match
    assert "Section A" in chunks[0].content


def test_chunker_drops_intro_above_first_h2() -> None:
    md = """# Title

This intro is not retrievable signal.

## Section A
Body.
"""
    chunks = chunk_markdown(md, doc="x.md")
    assert len(chunks) == 1
    assert "intro" not in chunks[0].content.lower()


def test_chunker_skips_empty_sections() -> None:
    md = "## Empty\n\n## Has Body\n\nReal content here."
    chunks = chunk_markdown(md, doc="x.md")
    assert len(chunks) == 1
    assert chunks[0].section == "Has Body"


def test_chunker_chunk_id_is_deterministic_slug() -> None:
    md = "## UAE passport — Japan\n\nContent."
    chunks = chunk_markdown(md, doc="visa_rules.md")
    assert chunks[0].id == "visa_rules.md#uae-passport-japan"


def test_chunker_returns_empty_when_no_h2() -> None:
    assert chunk_markdown("# Just a title\n\nbody only", doc="x.md") == []


# ---------------------------------------------------------------------------
# Real KB load
# ---------------------------------------------------------------------------


def test_real_kb_loads_and_produces_expected_chunk_count() -> None:
    settings = get_settings()
    chunks = load_kb_chunks(settings.kb_dir)
    # 3 KB files; rough lower bound on H2 sections across them
    assert len(chunks) >= 15
    # Every doc represented
    docs = {c.doc for c in chunks}
    assert docs == {"visa_rules.md", "refund_policy.md", "baggage_policy.md"}


def test_kb_content_hash_changes_when_content_changes(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("## section\nbody")
    h1 = kb_content_hash(tmp_path)
    (tmp_path / "a.md").write_text("## section\ndifferent body")
    h2 = kb_content_hash(tmp_path)
    assert h1 != h2


# ---------------------------------------------------------------------------
# Retriever — search behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def retriever(tmp_path: Path) -> KBRetriever:
    settings = get_settings()
    return KBRetriever(
        kb_dir=settings.kb_dir,
        embeddings=MockEmbeddingsClient(),
        cache_dir=tmp_path / ".faiss_index",
    )


def test_retriever_finds_relevant_chunk_for_uae_japan_visa(retriever: KBRetriever) -> None:
    results = retriever.search(
        "UAE passport holders Japan visa-free tourism", top_k=3, min_score=0.0
    )
    assert results, "expected at least one match"
    top = results[0]
    assert top.doc == "visa_rules.md"
    assert "UAE passport — Japan" in top.section or "japan" in top.section.lower()
    assert top.score is not None and top.score > 0


def test_retriever_threshold_gate_filters_irrelevant(retriever: KBRetriever) -> None:
    """Query so unrelated to the KB that nothing should pass a high threshold."""
    results = retriever.search("quantum chromodynamics renormalization group", top_k=4, min_score=0.5)
    assert results == [], f"expected no results, got {[c.section for c in results]}"


def test_retriever_results_are_score_descending(retriever: KBRetriever) -> None:
    results = retriever.search("refund policy cancellation fee", top_k=4, min_score=0.0)
    if len(results) >= 2:
        scores = [c.score for c in results if c.score is not None]
        assert scores == sorted(scores, reverse=True)


def test_retriever_top_k_bound(retriever: KBRetriever) -> None:
    results = retriever.search("baggage", top_k=2, min_score=0.0)
    assert len(results) <= 2


def test_retriever_cache_reuse_across_instances(tmp_path: Path) -> None:
    """A second retriever pointing at the same cache dir should NOT re-embed."""
    settings = get_settings()
    cache_dir = tmp_path / ".faiss_index"

    embed_calls = {"count": 0}

    class CountingEmbeddings(MockEmbeddingsClient):
        def embed(self, texts: list[str]) -> list[list[float]]:
            embed_calls["count"] += 1
            return super().embed(texts)

    r1 = KBRetriever(
        kb_dir=settings.kb_dir,
        embeddings=CountingEmbeddings(),
        cache_dir=cache_dir,
    )
    r1.build_or_load()
    first_call_count = embed_calls["count"]
    assert first_call_count >= 1, "expected at least one embed call to populate cache"

    # Fresh retriever, same cache dir, same KB hash → must NOT re-embed chunks.
    r2 = KBRetriever(
        kb_dir=settings.kb_dir,
        embeddings=CountingEmbeddings(),
        cache_dir=cache_dir,
    )
    r2.build_or_load()
    # Build itself is the chunk-embedding pass; query embedding comes later via search().
    # We expect zero additional chunk-embedding calls during build_or_load on r2.
    second_round_calls = embed_calls["count"] - first_call_count
    assert second_round_calls == 0, (
        f"cache reuse failed: re-embedded chunks on r2 (extra calls = {second_round_calls})"
    )
