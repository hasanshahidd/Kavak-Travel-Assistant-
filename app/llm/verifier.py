"""Citation verifier - the second half of the no-hallucination architecture.

The first half is the retrieval threshold gate (``KBRetriever.search`` returns
``[]`` when nothing scores above 0.5, forcing the answerer to refuse). This
module is what fires when retrieval *did* return chunks but the model still
made things up.

Contract:

* Every ``Citation`` in a ``RagAnswer`` is checked against the chunk it claims
  to cite. Specifically: the citation's ``span`` must appear as a substring
  (after light whitespace normalisation) in some chunk whose ``doc`` matches.
* Citations that fail this check are stripped - silently, without correcting.
  The model gets one chance; we don't try to fix its homework.
* If verification removes *all* citations on a non-refusal answer, the answer
  is converted into an explicit refusal so the user never sees an
  unsupported claim.

Design notes:

* **Whitespace normalisation** before matching, so trivial reformatting
  (line wraps, double-spaces) doesn't kill a perfectly-good citation.
* **Lowercased** doc-name matching, since the schema already lowercases.
* **No fuzzy matching.** Partial / approximate / "close enough" matches are
  exactly the kind of slippery-slope behaviour that lets hallucinations
  through. Verbatim or it's stripped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.rag import Chunk, Citation, RagAnswer

_WHITESPACE = re.compile(r"\s+")

REFUSAL_TEMPLATE = (
    "I don't have information about that in my knowledge base. The policies I "
    "do have cover visa rules, refund policy, and baggage policy for a small "
    "set of common routes."
)


# ---------------------------------------------------------------------------
# Verification report - useful for tracing AND for tests
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VerificationReport:
    """Returned alongside the verified answer for trace logging.

    Captures *what* the verifier did so the trace explains why some
    citations disappeared between the model's output and the user's reply.
    """

    kept: list[Citation]
    stripped: list[Citation]
    converted_to_refusal: bool

    @property
    def violation_count(self) -> int:
        return len(self.stripped)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Collapse runs of whitespace to a single space and lowercase. Used for substring matching."""
    return _WHITESPACE.sub(" ", text).strip().lower()


def _index_chunks_by_doc(chunks: list[Chunk]) -> dict[str, list[str]]:
    """Group normalized chunk content by doc name for O(1) lookup during verification."""
    index: dict[str, list[str]] = {}
    for c in chunks:
        index.setdefault(c.doc.lower(), []).append(_normalize(c.content))
    return index


def _is_span_grounded(span: str, normalized_corpus_for_doc: list[str]) -> bool:
    """True iff the span appears as a substring of at least one normalized chunk for this doc."""
    needle = _normalize(span)
    if not needle:
        return False
    return any(needle in haystack for haystack in normalized_corpus_for_doc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_citations(
    answer: RagAnswer,
    chunks: list[Chunk],
) -> tuple[RagAnswer, VerificationReport]:
    """Strip unverified citations; convert to refusal if all citations fail.

    Returns the verified ``RagAnswer`` plus a :class:`VerificationReport` so
    the tracer can log exactly which citations were stripped and why. The
    input ``answer`` is NOT mutated.

    Refusal answers (``answer.is_refusal=True``) pass through unchanged.
    """
    if answer.is_refusal:
        return answer, VerificationReport(kept=list(answer.citations), stripped=[], converted_to_refusal=False)

    corpus = _index_chunks_by_doc(chunks)

    kept: list[Citation] = []
    stripped: list[Citation] = []
    for citation in answer.citations:
        doc_chunks = corpus.get(citation.doc.lower())
        if not doc_chunks:
            stripped.append(citation)
            continue
        if _is_span_grounded(citation.span, doc_chunks):
            kept.append(citation)
        else:
            stripped.append(citation)

    if kept:
        verified = answer.model_copy(update={"citations": kept})
        return verified, VerificationReport(
            kept=kept, stripped=stripped, converted_to_refusal=False
        )

    # All citations stripped on a non-refusal answer → convert to refusal
    # rather than risk shipping unsupported claims.
    refusal = RagAnswer(
        answer=REFUSAL_TEMPLATE,
        citations=[],
        confidence=0.0,
        is_refusal=True,
    )
    return refusal, VerificationReport(
        kept=[], stripped=stripped, converted_to_refusal=True
    )


__all__ = [
    "REFUSAL_TEMPLATE",
    "VerificationReport",
    "verify_citations",
]
