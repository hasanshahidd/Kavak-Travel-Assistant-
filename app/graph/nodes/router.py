"""Router graph node - classifies user intent.

Pure callable: ``route(message, summary, client, tracer) -> RouterOutput``.
Block 6's ``builder.py`` adapts this to LangGraph's ``(state) -> dict``
contract; keeping the inner function pure makes it trivially testable.
"""

from __future__ import annotations

import time

from app.llm.client import LLMClient
from app.llm.prompt_loader import load_prompt
from app.llm.tracing import Tracer
from app.schemas.intent import RouterOutput

PROMPT_NAME = "router"


def route(
    *,
    user_message: str,
    conversation_summary: str,
    client: LLMClient,
    tracer: Tracer | None = None,
) -> RouterOutput:
    """Run the router prompt and return the classified intent + rationale."""
    started = time.perf_counter()
    prompt = load_prompt(PROMPT_NAME)
    response = client.complete(
        prompt=prompt,
        response_model=RouterOutput,
        variables={
            "user_message": user_message,
            "conversation_summary": conversation_summary,
        },
    )
    output = response.data
    if not isinstance(output, RouterOutput):
        raise TypeError(f"router expected RouterOutput, got {type(output).__name__}")

    if tracer is not None:
        tracer.emit(
            node="router",
            prompt_id=response.prompt_id,
            prompt_hash=response.prompt_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.usage.tokens_in,
            tokens_out=response.usage.tokens_out,
            cost_usd=response.cost_usd,
            output={"intent": output.intent.value, "rationale": output.rationale},
        )
    return output


__all__ = ["PROMPT_NAME", "route"]
