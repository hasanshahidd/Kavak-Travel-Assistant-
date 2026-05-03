"""Clarifier graph node — asks ONE targeted question for missing fields."""

from __future__ import annotations

import time

from app.llm.client import LLMClient
from app.llm.prompt_loader import load_prompt
from app.llm.tracing import Tracer

PROMPT_NAME = "clarifier"


def clarify(
    *,
    user_message: str,
    conversation_summary: str,
    missing_fields: list[str],
    client: LLMClient,
    tracer: Tracer | None = None,
) -> str:
    """Generate a single-question clarification reply.

    Output is plain text — no schema. The clarifier prompt enforces the
    one-question rule.
    """
    started = time.perf_counter()
    prompt = load_prompt(PROMPT_NAME)
    response = client.complete(
        prompt=prompt,
        variables={
            "user_message": user_message,
            "conversation_summary": conversation_summary,
            "missing_fields": ", ".join(missing_fields) if missing_fields else "(none)",
        },
    )
    text = response.data if isinstance(response.data, str) else str(response.data)

    if tracer is not None:
        tracer.emit(
            node="clarifier",
            prompt_id=response.prompt_id,
            prompt_hash=response.prompt_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.usage.tokens_in,
            tokens_out=response.usage.tokens_out,
            cost_usd=response.cost_usd,
            output={"missing_fields": missing_fields, "question_preview": text[:200]},
        )
    return text


__all__ = ["PROMPT_NAME", "clarify"]
