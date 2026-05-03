"""End-to-end RAG integration tests.

These tests are the proof that **every block built so far wires together**:

  Block 1 schemas    → ``Chunk``, ``Citation``, ``RagAnswer``
  Block 2 substrate  → prompt loader, LLM client, tracer
  Block 3 prompts    → ``rag_answer.md`` (loaded, rendered with chunk context)
  Block 4 RAG        → KB retriever, citation verifier, graph nodes

If any wiring breaks, these tests fail. They use ``MockClient`` and
``MockEmbeddingsClient`` so they run without network or API keys; the
opt-in OpenAI smoke at the bottom proves the same flow works against
the real API when the key is present.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import get_settings
from app.graph.nodes.answerer import answer
from app.graph.nodes.retriever import retrieve
from app.llm.client import MockClient
from app.llm.embeddings import MockEmbeddingsClient
from app.llm.tracing import Tracer
from app.schemas.rag import Citation, RagAnswer
from app.tools.kb_retriever import KBRetriever


@pytest.fixture
def kb(tmp_path: Path) -> KBRetriever:
    settings = get_settings()
    return KBRetriever(
        kb_dir=settings.kb_dir,
        embeddings=MockEmbeddingsClient(),
        cache_dir=tmp_path / ".faiss_index",
    )


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer(turn_id="rag-int-001", trace_dir=tmp_path / "traces", redact=True)


# ---------------------------------------------------------------------------
# Path 1 - happy path with real KB + real prompt + verified citation
# ---------------------------------------------------------------------------


def test_full_rag_path_visa_question(kb: KBRetriever, tracer: Tracer, tmp_path: Path) -> None:
    """End-to-end: visa question → retrieve → answer → verify → traced."""
    question = "Do UAE passport holders need a visa for Japan for tourism?"

    # Step 1: retrieve
    chunks = retrieve(question=question, kb=kb, tracer=tracer, min_score=0.0)
    assert chunks, "retrieval should find UAE/Japan visa chunk"
    assert any("japan" in c.section.lower() for c in chunks)

    # Step 2: register a canned LLM response that cites a verbatim span
    # from the actual KB content. This proves the integration in two ways:
    #  - The MockClient interface works with the rag_answer prompt
    #  - The verifier confirms the cited span is actually in the retrieved chunk
    # Pick the UAE→Japan chunk specifically - the KB now has multiple
    # Japan visa rules (UAE / Indian / UK / Pakistani / Filipino passports).
    visa_chunk = next(
        c for c in chunks
        if c.doc == "visa_rules.md"
        and "japan" in c.section.lower()
        and "uae" in c.section.lower()
    )
    cited_span = "visa-free for tourism for up to 30 days"
    assert cited_span in visa_chunk.content, "test fixture mismatch with real KB"

    canned = RagAnswer(
        answer=(
            "UAE passport holders can enter Japan visa-free for tourism for up to 30 days, "
            "provided their passport is valid for at least 6 months from arrival."
        ),
        citations=[Citation(doc="visa_rules.md", span=cited_span)],
        confidence=0.9,
    )
    client = MockClient()
    client.register("rag_answer.v3", raw_text=canned.model_dump_json(), parsed=canned)

    # Step 3: answer (loads prompt, calls client, verifies citations, traces)
    result = answer(question=question, chunks=chunks, client=client, tracer=tracer)

    assert isinstance(result, RagAnswer)
    assert not result.is_refusal
    assert len(result.citations) == 1
    assert result.citations[0].doc == "visa_rules.md"
    assert "30 days" in result.answer

    # Step 4: trace contains both nodes with their wiring details
    nodes = [e.node for e in tracer.events]
    assert nodes == ["retriever", "answerer"]

    retriever_event = tracer.events[0]
    assert retriever_event.output["below_threshold"] is False
    assert retriever_event.output["result_count"] == len(chunks)

    answerer_event = tracer.events[1]
    assert answerer_event.output["path"] == "rag"
    assert answerer_event.output["verifier"]["citations_kept"] == 1
    assert answerer_event.output["verifier"]["citations_stripped"] == 0
    assert answerer_event.prompt_id == "rag_answer.v3"  # ties to the prompt CHANGELOG entry

    # Trace persisted to disk
    trace_files = list((tmp_path / "traces").rglob("*.jsonl"))
    assert trace_files, "tracer must have written a JSONL file"


# ---------------------------------------------------------------------------
# Path 2 - verifier strips a hallucinated span → answer becomes a refusal
# ---------------------------------------------------------------------------


def test_hallucinated_span_is_stripped_and_converted_to_refusal(
    kb: KBRetriever, tracer: Tracer
) -> None:
    """Model invents a citation span. Verifier strips it. User sees a refusal, not a lie."""
    question = "Do UAE passport holders need a visa for Japan?"
    chunks = retrieve(question=question, kb=kb, tracer=tracer, min_score=0.0)

    # Hallucinated answer: the cited span sounds plausible but isn't actually in the KB.
    hallucinated = RagAnswer(
        answer="UAE passport holders need a B-1 tourist visa for Japan (totally made up).",
        citations=[
            Citation(
                doc="visa_rules.md",
                span="B-1 tourist visa is required for UAE passport holders visiting Japan",
            ),
        ],
        confidence=0.9,
    )
    client = MockClient()
    client.register("rag_answer.v3", raw_text=hallucinated.model_dump_json(), parsed=hallucinated)

    result = answer(question=question, chunks=chunks, client=client, tracer=tracer)

    # Verifier did its job: structurally impossible to ship a hallucination here.
    assert result.is_refusal is True
    assert "B-1 tourist visa" not in result.answer
    assert result.citations == []

    # The trace records WHY the conversion happened - useful for debugging.
    answerer_event = tracer.events[-1]
    assert answerer_event.output["verifier"]["converted_to_refusal"] is True
    assert answerer_event.output["verifier"]["citations_stripped"] == 1


# ---------------------------------------------------------------------------
# Path 3 - retrieval misses → structural refusal (no LLM call)
# ---------------------------------------------------------------------------


def test_off_topic_question_short_circuits_to_refusal(kb: KBRetriever, tracer: Tracer) -> None:
    """Question with nothing relevant in the KB → refusal without spending tokens."""
    question = "What's the visa requirement for Atlantis?"

    chunks = retrieve(question=question, kb=kb, tracer=tracer, min_score=0.5)
    assert chunks == [], "off-topic query should return no chunks above threshold"

    # MockClient is intentionally NOT registered. If the answerer wrongly calls
    # the LLM, it would default to plain text and fail validation. The fact
    # that this test passes means the structural-refusal short-circuit fires.
    client = MockClient()
    result = answer(question=question, chunks=chunks, client=client, tracer=tracer)

    assert result.is_refusal is True
    assert result.citations == []

    answerer_event = tracer.events[-1]
    assert answerer_event.output["path"] == "structural_refusal"
    assert answerer_event.tokens_in == 0  # no LLM call → zero tokens consumed
    assert answerer_event.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Path 4 - opt-in real OpenAI integration
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY") or os.environ.get("SKIP_OPENAI_INTEGRATION") == "1",
    reason="No OPENAI_API_KEY or explicitly skipped",
)
def test_full_rag_path_with_real_openai(tmp_path: Path) -> None:
    """Live smoke test against real OpenAI. Skipped by default."""
    from app.llm.client import OpenAIClient
    from app.llm.embeddings import OpenAIEmbeddingsClient

    settings = get_settings()
    kb = KBRetriever(
        kb_dir=settings.kb_dir,
        embeddings=OpenAIEmbeddingsClient(),
        cache_dir=tmp_path / ".faiss_index",
    )
    tracer = Tracer(turn_id="rag-int-live", trace_dir=tmp_path / "traces", redact=True)
    client = OpenAIClient()

    question = "Do UAE passport holders need a visa for Japan?"
    chunks = retrieve(question=question, kb=kb, tracer=tracer, min_score=0.4)
    assert chunks, "real embeddings should still find the visa chunk"

    result = answer(question=question, chunks=chunks, client=client, tracer=tracer)
    # If the model tried to hallucinate, the verifier would strip the citations
    # and convert to refusal. Either is acceptable end-state - a real-OpenAI
    # answer that survives verification, or an honest refusal.
    if result.is_refusal:
        assert result.citations == []
    else:
        assert result.citations
        for c in result.citations:
            # Verbatim check: every shipped citation must be a substring of a chunk.
            assert any(c.span.lower() in chunk.content.lower() for chunk in chunks)
