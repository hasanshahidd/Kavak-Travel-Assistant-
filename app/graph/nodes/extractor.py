"""Extractor graph node - natural language → ``FlightQuery`` + memory merge.

Two responsibilities, in order:

1. **Call the extractor prompt** (``extractor.md``) which produces a fresh
   ``FlightQuery`` from the user message + conversation summary. The
   prompt's few-shots already cover override and topic-switch patterns;
   the model usually gets it right.

2. **Apply the deterministic merge** (:func:`app.memory.conversation.merge_query`)
   as a safety net. The merge handles the cases where the model emitted
   ``None`` for fields the user didn't restate but clearly still wants
   ("make it cheaper" - destination etc. should carry over).

The merge is logged in the trace so a reviewer can see when memory
inheritance kicked in vs. when the model's output was used as-is.
"""

from __future__ import annotations

import time

from app.llm.client import LLMClient
from app.llm.prompt_loader import load_prompt
from app.llm.tracing import Tracer
from app.memory.conversation import is_topic_switch, merge_query
from app.schemas.flight import FlightQuery

PROMPT_NAME = "extractor"


def extract(
    *,
    user_message: str,
    conversation_summary: str,
    prior_query: FlightQuery | None,
    client: LLMClient,
    tracer: Tracer | None = None,
) -> FlightQuery:
    """Extract a FlightQuery, merging with prior conversation state.

    The returned query is what the flight tool consumes and what gets
    stored back into memory after the turn.
    """
    started = time.perf_counter()
    prompt = load_prompt(PROMPT_NAME)
    response = client.complete(
        prompt=prompt,
        response_model=FlightQuery,
        variables={
            "user_message": user_message,
            "conversation_summary": conversation_summary,
        },
    )
    raw_query = response.data
    if not isinstance(raw_query, FlightQuery):
        raise TypeError(f"extractor expected FlightQuery, got {type(raw_query).__name__}")

    # Apply deterministic merge against prior state.
    merged = merge_query(prior_query, raw_query)
    detected_switch = is_topic_switch(prior_query, raw_query) if prior_query else False

    if tracer is not None:
        tracer.emit(
            node="extractor",
            prompt_id=response.prompt_id,
            prompt_hash=response.prompt_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.usage.tokens_in,
            tokens_out=response.usage.tokens_out,
            cost_usd=response.cost_usd,
            output={
                "raw_query": raw_query.model_dump(mode="json"),
                "merged_query": merged.model_dump(mode="json"),
                "had_prior": prior_query is not None,
                "topic_switch": detected_switch,
                "needs_clarification": merged.needs_clarification,
                "missing_fields": merged.missing_fields,
            },
        )
    return merged


__all__ = ["PROMPT_NAME", "extract"]
