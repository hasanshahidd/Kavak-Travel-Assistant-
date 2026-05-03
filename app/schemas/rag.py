"""RAG schemas - chunks, citations, answers.

The post-processor in ``app.llm.verifier`` enforces that every ``Citation.span``
appears verbatim in the named source document. If verification fails, the answer
is replaced with a refusal - making hallucination structurally impossible.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Chunk(BaseModel):
    """One retrievable piece of the knowledge base.

    Chunked by H2 section so each Chunk is self-contained: a single visa rule,
    refund clause, or baggage limit. The ``content`` field includes the heading
    text so a query like "UAE Japan visa" can match either the heading or body.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        description="Stable within-corpus id, e.g. 'visa_rules.md#uae-passport-japan'.",
        min_length=1,
    )
    doc: str = Field(
        ...,
        description="Source filename, e.g. 'visa_rules.md'.",
        min_length=1,
    )
    section: str = Field(
        ...,
        description="H2 heading text, e.g. 'UAE passport - Japan'.",
        min_length=1,
    )
    content: str = Field(
        ...,
        description="Full section text including heading. Used for retrieval AND citation verification.",
        min_length=8,
    )
    score: float | None = Field(
        None,
        description="Cosine similarity vs. the query, populated by the retriever. None for raw chunks.",
    )


class Citation(BaseModel):
    """A traceable claim - must point to a real substring of a real source."""

    model_config = ConfigDict(extra="forbid")

    doc: str = Field(
        ...,
        description="Source document filename, e.g. 'visa_rules.md'.",
        min_length=1,
    )
    span: str = Field(
        ...,
        description=(
            "Verbatim substring of the source. Verifier checks `span in source_text`."
        ),
        min_length=8,
        max_length=400,
    )

    @field_validator("doc")
    @classmethod
    def _normalize_doc(cls, v: str) -> str:
        return v.strip().lower()


class RagAnswer(BaseModel):
    """RAG response with citation enforcement built into the type."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(..., min_length=1, description="User-facing answer.")
    citations: list[Citation] = Field(
        ...,
        description=(
            "MUST be non-empty when the answer makes a factual claim. "
            "Empty list is only valid when answer is an explicit refusal."
        ),
    )
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="Self-reported confidence. Used to gate borderline answers.",
    )
    is_refusal: bool = Field(
        False,
        description=(
            "True when the model declined to answer (e.g. low retrieval relevance). "
            "When true, citations may be empty."
        ),
    )

    @field_validator("citations")
    @classmethod
    def _no_blank_spans(cls, v: list[Citation]) -> list[Citation]:
        return [c for c in v if c.span.strip()]
