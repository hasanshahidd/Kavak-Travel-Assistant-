"""Citation verifier tests.

The verifier is the second half of the no-hallucination architecture.
These tests prove:

* Verbatim spans pass.
* Whitespace-tolerant matching (line wraps don't kill citations).
* Wrong doc names fail.
* Paraphrased / fabricated spans fail.
* When ALL citations strip on a non-refusal answer, the verifier
  converts the answer into an explicit refusal.
* Refusal answers pass through untouched.
"""

from __future__ import annotations

import pytest

from app.llm.verifier import REFUSAL_TEMPLATE, verify_citations
from app.schemas.rag import Chunk, Citation, RagAnswer

# ---------------------------------------------------------------------------
# Fixture chunks — drawn from the real KB style so realism matters
# ---------------------------------------------------------------------------


@pytest.fixture
def visa_chunk() -> Chunk:
    return Chunk(
        id="visa_rules.md#uae-passport-japan",
        doc="visa_rules.md",
        section="UAE passport — Japan",
        content=(
            "UAE passport — Japan\n\n"
            "UAE passport holders can enter Japan visa-free for tourism for up to 30 days. "
            "Passport must be valid for at least 6 months from arrival, and proof of "
            "onward travel may be requested at immigration."
        ),
    )


@pytest.fixture
def refund_chunk() -> Chunk:
    return Chunk(
        id="refund_policy.md#refundable-tickets-cancellation",
        doc="refund_policy.md",
        section="Refundable tickets — cancellation",
        content=(
            "Refundable tickets — cancellation\n\n"
            "Refundable tickets can be cancelled up to 48 hours before the scheduled "
            "departure time. A processing fee of 10% of the ticket price applies."
        ),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_verbatim_citation_passes(visa_chunk: Chunk) -> None:
    answer = RagAnswer(
        answer="UAE passport holders can enter Japan visa-free for 30 days.",
        citations=[
            Citation(doc="visa_rules.md", span="visa-free for tourism for up to 30 days"),
        ],
        confidence=0.9,
    )
    verified, report = verify_citations(answer, [visa_chunk])
    assert len(verified.citations) == 1
    assert report.violation_count == 0
    assert not report.converted_to_refusal


def test_whitespace_tolerant_matching(visa_chunk: Chunk) -> None:
    """Line wraps and double spaces in the cited span shouldn't kill the match."""
    answer = RagAnswer(
        answer="UAE passport holders can enter Japan visa-free.",
        citations=[
            Citation(
                doc="visa_rules.md",
                span="UAE  passport holders can enter\nJapan visa-free  for tourism",
            ),
        ],
        confidence=0.9,
    )
    verified, report = verify_citations(answer, [visa_chunk])
    assert len(verified.citations) == 1
    assert report.violation_count == 0


def test_doc_name_case_insensitive(visa_chunk: Chunk) -> None:
    answer = RagAnswer(
        answer="OK.",
        citations=[
            Citation(doc="VISA_RULES.MD", span="UAE passport holders can enter Japan"),
        ],
        confidence=0.9,
    )
    verified, _ = verify_citations(answer, [visa_chunk])
    assert len(verified.citations) == 1


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_paraphrased_span_is_stripped(visa_chunk: Chunk) -> None:
    """Citation says something CLOSE to the chunk but not verbatim → strip it."""
    answer = RagAnswer(
        answer="UAE travelers visit Japan visa-free for one month.",  # paraphrased
        citations=[
            Citation(
                doc="visa_rules.md",
                # Original says "30 days"; this says "one month" — close, not verbatim.
                span="UAE travelers can visit Japan visa-free for one month",
            ),
        ],
        confidence=0.9,
    )
    verified, report = verify_citations(answer, [visa_chunk])
    assert verified.is_refusal, "all citations stripped → should convert to refusal"
    assert report.violation_count == 1


def test_wrong_doc_name_is_stripped(visa_chunk: Chunk, refund_chunk: Chunk) -> None:
    """Span exists in chunk A but is cited as coming from chunk B's doc."""
    answer = RagAnswer(
        answer="Visa policy stuff.",
        citations=[
            Citation(doc="refund_policy.md", span="UAE passport holders can enter Japan"),
        ],
        confidence=0.9,
    )
    verified, report = verify_citations(answer, [visa_chunk, refund_chunk])
    assert verified.is_refusal
    assert report.violation_count == 1


def test_all_citations_invalid_converts_to_refusal(visa_chunk: Chunk) -> None:
    answer = RagAnswer(
        answer="Made-up claim about something not in the chunk at all.",
        citations=[
            Citation(doc="visa_rules.md", span="this exact text is nowhere in the chunk"),
            Citation(doc="visa_rules.md", span="another invented quote we cannot verify"),
        ],
        confidence=0.85,
    )
    verified, report = verify_citations(answer, [visa_chunk])
    assert verified.is_refusal is True
    assert verified.answer == REFUSAL_TEMPLATE
    assert verified.citations == []
    assert verified.confidence == 0.0
    assert report.converted_to_refusal is True
    assert report.violation_count == 2


def test_partial_strip_keeps_valid_citations(visa_chunk: Chunk, refund_chunk: Chunk) -> None:
    """Mix one valid + one invalid citation → keep valid, strip invalid, no refusal."""
    answer = RagAnswer(
        answer="Some answer with two claims, one supported.",
        citations=[
            Citation(doc="visa_rules.md", span="visa-free for tourism for up to 30 days"),
            Citation(doc="visa_rules.md", span="invented claim that isn't in the source"),
        ],
        confidence=0.8,
    )
    verified, report = verify_citations(answer, [visa_chunk, refund_chunk])
    assert verified.is_refusal is False
    assert len(verified.citations) == 1
    assert verified.citations[0].span == "visa-free for tourism for up to 30 days"
    assert report.violation_count == 1


def test_refusal_answer_passes_through_untouched(visa_chunk: Chunk) -> None:
    answer = RagAnswer(
        answer="I don't have information about that.",
        citations=[],
        confidence=0.0,
        is_refusal=True,
    )
    verified, report = verify_citations(answer, [visa_chunk])
    assert verified == answer
    assert report.kept == []
    assert report.stripped == []
    assert not report.converted_to_refusal


def test_no_chunks_provided_strips_everything() -> None:
    answer = RagAnswer(
        answer="UAE visa info.",
        citations=[Citation(doc="visa_rules.md", span="some span over 8 chars long")],
        confidence=0.9,
    )
    verified, report = verify_citations(answer, [])
    assert verified.is_refusal
    assert report.converted_to_refusal
