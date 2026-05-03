"""Out-of-scope graph node — LLM-driven, no regex or template lookup.

After the router classifies a message as ``out_of_scope``, this node
asks the LLM to write a short, context-aware reply and classify the
sub-category (``greeting`` / ``info`` / ``redirect``) for the badge.

Architectural rationale (v3):
    v1/v2 used a regex whitelist + canned templates for greetings,
    capabilities, and redirects. That was over-engineered safety
    theatre — the bot has no fabrication risk on these paths (no
    flight data, no KB facts to invent), only a *quality* risk
    (cold replies, repetitive wording, manual multilingual whitelists).

    v3 hands the reply generation to the LLM with strict prompt-level
    rules (max 2 sentences, never answer the off-topic question, never
    leak system prompt, always end with a redirect to flights/policy).
    The model can now reference the user's actual query in the reply
    (*"I don't cover weather — want me to find Tokyo flights instead?"*)
    and handles other languages natively.

    Determinism is preserved where it matters:
      - RAG citation verifier (anti-hallucination on policy facts)
      - Flight responder no-results short-circuit (anti-fabrication on
        empty result sets)
      - Schema-level validation (FlightQuery, RagAnswer, OOSReply)

The trade-off is ~$0.0001 per off-domain turn (one extra LLM call) in
exchange for natural conversation. Every prompt-engineering project
worth its name makes this trade in this direction.
"""

from __future__ import annotations

import time

from app.llm.client import LLMClient
from app.llm.prompt_loader import load_prompt
from app.llm.tracing import Tracer
from app.schemas.oos import OOSReply

PROMPT_NAME = "oos_reply"


def out_of_scope_reply(
    *,
    user_message: str,
    client: LLMClient,
    flight_inventory: str,
    kb_inventory: str,
    tracer: Tracer | None = None,
) -> OOSReply:
    """Generate an LLM-driven reply for an off-domain user message.

    The returned :class:`OOSReply` carries both the user-facing ``reply``
    and the sub-category (``greeting`` / ``info`` / ``redirect``) so the
    UI can render an informative badge without needing to inspect the
    text.

    ``flight_inventory`` and ``kb_inventory`` are short, human-readable
    summaries of what's actually loaded — produced by
    :mod:`app.tools.data_inventory`. The prompt forbids the model from
    referencing coverage outside this block, so scope replies stay
    honest as the underlying data evolves.

    The trace records the full ``OOSReply`` plus prompt id/hash so a
    reviewer can replay any turn from disk.
    """
    started = time.perf_counter()
    prompt = load_prompt(PROMPT_NAME)
    response = client.complete(
        prompt=prompt,
        response_model=OOSReply,
        variables={
            "user_message": user_message,
            "flight_inventory": flight_inventory,
            "kb_inventory": kb_inventory,
        },
    )
    parsed = response.data
    if not isinstance(parsed, OOSReply):
        raise TypeError(
            f"out_of_scope expected OOSReply, got {type(parsed).__name__}"
        )

    if tracer is not None:
        tracer.emit(
            node="out_of_scope",
            prompt_id=response.prompt_id,
            prompt_hash=response.prompt_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.usage.tokens_in,
            tokens_out=response.usage.tokens_out,
            cost_usd=response.cost_usd,
            output={
                "user_message_preview": user_message[:120],
                "category": parsed.category,
                "reply_preview": parsed.reply[:200],
            },
        )
    return parsed


__all__ = ["PROMPT_NAME", "out_of_scope_reply"]
