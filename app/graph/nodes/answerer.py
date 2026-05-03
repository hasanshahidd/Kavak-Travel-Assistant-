"""Answerer graph node - composes a verified ``RagAnswer`` from chunks.

Wires together every Block 1-3 substrate:

* :func:`app.llm.prompt_loader.load_prompt` - loads ``rag_answer.md`` (Block 3)
* :class:`app.llm.client.LLMClient`        - provider-agnostic call (Block 2)
* :func:`app.llm.verifier.verify_citations` - citation verification (Block 4)
* :class:`app.llm.tracing.Tracer`          - observability (Block 2)
* :class:`app.schemas.rag.RagAnswer`       - strict typed output (Block 1)

The function is intentionally a single readable flow: format chunks for the
prompt, call the model, verify citations, log the result. No hidden state.
"""

from __future__ import annotations

import time

from app.llm.client import LLMClient
from app.llm.prompt_loader import load_prompt
from app.llm.tracing import Tracer
from app.llm.verifier import REFUSAL_TEMPLATE, verify_citations
from app.schemas.rag import Chunk, RagAnswer

PROMPT_NAME = "rag_answer"


def _format_chunks_for_prompt(chunks: list[Chunk]) -> str:
    """Render retrieved chunks as a numbered, doc-tagged block for the prompt.

    Format chosen so the model can reference chunks naturally and cite the
    correct ``doc`` filename - matches the contract the rag_answer.md prompt
    sets up (citations must reference real docs).
    """
    if not chunks:
        return "[no chunks retrieved - refusal path]"
    lines: list[str] = []
    for i, c in enumerate(chunks, start=1):
        score_label = f"score={c.score:.3f}" if c.score is not None else "score=n/a"
        lines.append(f"### Chunk {i} (doc: {c.doc}, section: {c.section}, {score_label})")
        lines.append(c.content)
        lines.append("")
    return "\n".join(lines).rstrip()


def answer(
    *,
    question: str,
    chunks: list[Chunk],
    client: LLMClient,
    conversation_summary: str | None = None,
    tracer: Tracer | None = None,
) -> RagAnswer:
    """Compose, validate and verify a RAG answer.

    Three paths:
      1. **Empty chunks** → return a structural refusal without an LLM call.
         Saves cost and matches the prompt's ``is_refusal`` contract.
      2. **Chunks present** → call the LLM, then verify citations. Unverified
         spans are stripped; if all are stripped, the answer is converted
         to a refusal by the verifier.
      3. **LLM fails validation** → propagated up; the caller decides.

    ``conversation_summary``, when provided, is passed to the LLM as
    *interpretation context only* - the prompt requires every factual
    claim to be grounded in chunks. This bridges the case where the
    user's raw message is short ("tell me on Tokyo") and only makes sense
    relative to the prior turn ("what visas do you cover").
    """
    started = time.perf_counter()

    # Path 1: nothing to ground in → refuse without spending tokens.
    if not chunks:
        result = RagAnswer(
            answer=REFUSAL_TEMPLATE,
            citations=[],
            confidence=0.0,
            is_refusal=True,
        )
        if tracer is not None:
            tracer.emit(
                node="answerer",
                latency_ms=(time.perf_counter() - started) * 1000,
                output={
                    "path": "structural_refusal",
                    "reason": "no chunks above relevance threshold",
                    "answer": result.model_dump(),
                },
            )
        return result

    # Path 2: real RAG path. Load prompt (cached), call LLM, verify.
    prompt = load_prompt(PROMPT_NAME)
    summary_block = (
        conversation_summary.strip()
        if conversation_summary and not conversation_summary.strip().startswith("(")
        else "(no prior conversation)"
    )
    response = client.complete(
        prompt=prompt,
        response_model=RagAnswer,
        variables={
            "user_question": question,
            "retrieved_chunks": _format_chunks_for_prompt(chunks),
            "conversation_context": summary_block,
        },
    )
    raw_answer = response.data
    if not isinstance(raw_answer, RagAnswer):
        raise TypeError(
            f"answerer expected RagAnswer from client, got {type(raw_answer).__name__}"
        )

    verified, report = verify_citations(raw_answer, chunks)

    if tracer is not None:
        tracer.emit(
            node="answerer",
            prompt_id=response.prompt_id,
            prompt_hash=response.prompt_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.usage.tokens_in,
            tokens_out=response.usage.tokens_out,
            cost_usd=response.cost_usd,
            output={
                "path": "rag",
                "answer": verified.model_dump(),
                "verifier": {
                    "citations_kept": len(report.kept),
                    "citations_stripped": len(report.stripped),
                    "converted_to_refusal": report.converted_to_refusal,
                    "stripped_doc_spans": [
                        {"doc": s.doc, "span_preview": s.span[:80]} for s in report.stripped
                    ],
                },
            },
        )

    return verified


__all__ = ["PROMPT_NAME", "answer"]
