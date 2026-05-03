"""Flight responder graph node - composes the user-facing reply.

Two-step pipeline:

1. **Draft.** Render ``flight_responder.md`` with the search outcome,
   call the LLM, get a polished Markdown reply.

2. **(Optional) Self-critique.** When ``self_critique=True``, run
   ``responder_critique.md`` against the draft. If the critique flags
   issues, run a *revision* pass with the issues injected as feedback.
   This is the 0.01% upgrade - A/B-tested in the eval suite.

The self-critique loop is OFF by default so the eval can measure the
delta between with/without. Block 7 toggles it via env flag.

Wires together:
  Block 1 schemas    → ``FlightQuery``, ``SearchOutcome``, ``ResponseCritique``
  Block 2 substrate  → prompt loader, LLM client, tracer
  Block 3 prompts    → ``flight_responder.md``
  Block 5 prompts    → ``responder_critique.md``
"""

from __future__ import annotations

import os
import time

from app.llm.client import LLMClient
from app.llm.prompt_loader import load_prompt
from app.llm.tracing import Tracer
from app.schemas.flight import FlightQuery, ResponseCritique, SearchOutcome

RESPONDER_PROMPT = "flight_responder"
CRITIQUE_PROMPT = "responder_critique"

# Env flag for the eval suite. Defaults to off so cost stays predictable
# and the eval baseline measures the simpler responder.
SELF_CRITIQUE_ENV = "RESPONDER_SELF_CRITIQUE"


# ---------------------------------------------------------------------------
# Helpers - prompt-friendly formatting of search results
# ---------------------------------------------------------------------------


def _format_user_query(query: FlightQuery) -> str:
    """One-line summary of the structured query, for the responder prompt."""
    bits: list[str] = []
    if query.origin and query.destination:
        bits.append(f"{query.origin} → {query.destination}")
    elif query.destination:
        bits.append(f"to {query.destination}")
    if query.departure_date:
        bits.append(query.departure_date.strftime("%B %Y"))
    if query.preferred_alliances:
        bits.append("/".join(query.preferred_alliances))
    if query.excluded_alliances:
        bits.append(f"NOT {'/'.join(query.excluded_alliances)}")
    if query.excluded_airlines:
        bits.append(f"excl. {', '.join(query.excluded_airlines)}")
    if query.avoid_overnight_layovers:
        bits.append("no overnight layovers")
    if query.max_price_usd is not None:
        bits.append(f"under ${query.max_price_usd:.0f}")
    if query.refundable_only:
        bits.append("refundable only")
    if query.sort_by == "price":
        bits.append("sorted by lowest price")
    return ", ".join(bits) if bits else "(no specific constraints)"


def _format_relaxation(outcome: SearchOutcome) -> str:
    """Human-readable note about what was relaxed, or 'no relaxation needed'."""
    if not outcome.relaxed_constraints:
        return "No relaxation needed - all constraints satisfied."
    pretty = [c.replace("_", " ") for c in outcome.relaxed_constraints]
    return f"Relaxed to find matches: {', '.join(pretty)}."


def _format_results(outcome: SearchOutcome) -> str:
    """Render top results as a structured block the prompt can consume."""
    if not outcome.results:
        if outcome.no_results_reason:
            return f"[no matches] {outcome.no_results_reason}"
        return "[no matches]"
    lines: list[str] = []
    for i, r in enumerate(outcome.results, start=1):
        f = r.flight
        layover = (
            f"{f.layover_hours:.1f}h via {', '.join(f.layovers)}"
            f"{' - overnight' if f.is_overnight_layover else ''}"
            if f.layovers
            else "Direct"
        )
        return_part = f" → {f.return_date.isoformat()}" if f.return_date else ""
        lines.append(
            f"{i}. {f.airline} ({f.alliance or 'Independent'}) | "
            f"{f.origin} → {f.destination} | "
            f"{f.departure_date.isoformat()}{return_part} | "
            f"${f.price_usd:.0f} | {'refundable' if f.refundable else 'non-refundable'} | "
            f"{layover} | rationale: {r.explanation}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def respond(
    *,
    query: FlightQuery,
    outcome: SearchOutcome,
    client: LLMClient,
    tracer: Tracer | None = None,
    self_critique: bool | None = None,
) -> str:
    """Generate the user-facing flight reply, optionally with self-critique.

    ``self_critique`` precedence: explicit kwarg > ``RESPONDER_SELF_CRITIQUE`` env
    flag > False. Eval harness toggles via env so the same code path runs in
    both A/B conditions.
    """
    # SHORT-CIRCUIT: empty results → deterministic template, NO LLM call.
    # This is the structural defence against the no-results hallucination
    # mode where the model invents flights to "fill the void" when the
    # tool returned []. Same architectural pattern as the RAG answerer's
    # structural-refusal path: when there's no data, don't trust the model
    # to write text - use the diagnosis verbatim. Surfaced by real-mode
    # stress test where the bot fabricated 3 Garuda/Jetstar/Qantas flights
    # with 2023 dates after a Sydney-under-$200 search returned zero matches.
    if not outcome.results:
        return _no_results_template(query, outcome, tracer)

    enabled = self_critique if self_critique is not None else _critique_enabled_from_env()

    user_query_summary = _format_user_query(query)
    relaxation = _format_relaxation(outcome)
    results_block = _format_results(outcome)

    # ---- Step 1: draft ----
    draft = _generate_draft(
        client=client,
        tracer=tracer,
        user_query=user_query_summary,
        relaxation=relaxation,
        flight_results=results_block,
    )

    if not enabled:
        return draft

    # ---- Step 2: critique ----
    critique = _critique(
        client=client,
        tracer=tracer,
        user_query=user_query_summary,
        relaxation=relaxation,
        results_block=results_block,
        draft=draft,
    )
    if not critique.needs_revision:
        return draft

    # ---- Step 3: revision ----
    return _revise(
        client=client,
        tracer=tracer,
        user_query=user_query_summary,
        relaxation=relaxation,
        flight_results=results_block,
        prior_draft=draft,
        issues=critique.issues,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _critique_enabled_from_env() -> bool:
    return os.environ.get(SELF_CRITIQUE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _generate_draft(
    *,
    client: LLMClient,
    tracer: Tracer | None,
    user_query: str,
    relaxation: str,
    flight_results: str,
) -> str:
    started = time.perf_counter()
    prompt = load_prompt(RESPONDER_PROMPT)
    response = client.complete(
        prompt=prompt,
        variables={
            "user_query": user_query,
            "relaxation_summary": relaxation,
            "flight_results": flight_results,
        },
    )
    text = response.data if isinstance(response.data, str) else str(response.data)
    if tracer is not None:
        tracer.emit(
            node="responder.draft",
            prompt_id=response.prompt_id,
            prompt_hash=response.prompt_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.usage.tokens_in,
            tokens_out=response.usage.tokens_out,
            cost_usd=response.cost_usd,
            output={"draft_preview": text[:200]},
        )
    return text


def _critique(
    *,
    client: LLMClient,
    tracer: Tracer | None,
    user_query: str,
    relaxation: str,
    results_block: str,
    draft: str,
) -> ResponseCritique:
    started = time.perf_counter()
    prompt = load_prompt(CRITIQUE_PROMPT)
    response = client.complete(
        prompt=prompt,
        response_model=ResponseCritique,
        variables={
            "user_query": user_query,
            "relaxed_constraints": relaxation,
            "flights_data": results_block,
            "draft_reply": draft,
        },
    )
    parsed = response.data
    if not isinstance(parsed, ResponseCritique):
        raise TypeError(
            f"critique expected ResponseCritique, got {type(parsed).__name__}"
        )
    if tracer is not None:
        tracer.emit(
            node="responder.critique",
            prompt_id=response.prompt_id,
            prompt_hash=response.prompt_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.usage.tokens_in,
            tokens_out=response.usage.tokens_out,
            cost_usd=response.cost_usd,
            output={
                "needs_revision": parsed.needs_revision,
                "issues": parsed.issues,
                "confidence": parsed.confidence,
            },
        )
    return parsed


def _revise(
    *,
    client: LLMClient,
    tracer: Tracer | None,
    user_query: str,
    relaxation: str,
    flight_results: str,
    prior_draft: str,
    issues: list[str],
) -> str:
    """Re-run the responder with critique feedback injected as extra context.

    We reuse ``flight_responder.md`` and append the issue list as a short
    extra constraint section - no separate "revision" prompt needed. This
    keeps the prompt CHANGELOG focused on the *one* responder version while
    still allowing iterative quality.
    """
    started = time.perf_counter()
    prompt = load_prompt(RESPONDER_PROMPT)
    issues_block = "\n".join(f"- {issue}" for issue in issues)
    augmented_query = (
        f"{user_query}\n\n"
        f"[critique feedback on prior draft - address each before responding]\n"
        f"{issues_block}\n\n"
        f"[prior draft for context]\n{prior_draft}"
    )
    response = client.complete(
        prompt=prompt,
        variables={
            "user_query": augmented_query,
            "relaxation_summary": relaxation,
            "flight_results": flight_results,
        },
    )
    text = response.data if isinstance(response.data, str) else str(response.data)
    if tracer is not None:
        tracer.emit(
            node="responder.revision",
            prompt_id=response.prompt_id,
            prompt_hash=response.prompt_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens_in=response.usage.tokens_in,
            tokens_out=response.usage.tokens_out,
            cost_usd=response.cost_usd,
            output={
                "issues_addressed": issues,
                "revised_preview": text[:200],
            },
        )
    return text


def _no_results_template(
    query: FlightQuery,
    outcome: SearchOutcome,
    tracer: Tracer | None,
) -> str:
    """Deterministic no-LLM reply for the empty-results path.

    The model is allowed to hallucinate flights to "fill the void" if asked
    to write free text on top of an empty result set. This template never
    calls the LLM - it surfaces the flight tool's diagnosis verbatim with
    a polite intro. Zero hallucination risk by construction.

    Mirror of the RAG answerer's structural-refusal path.
    """
    started = time.perf_counter()

    reason = outcome.no_results_reason or (
        "I couldn't find any flights matching all of your criteria."
    )

    # Compose deterministic plain-text reply. No model in the loop.
    parts: list[str] = ["**No flights matched your search.**", "", reason]

    # If there's a clear single relaxation suggestion in the reason, leave
    # it as is. The diagnoser already phrased it well.
    text = "\n".join(parts).rstrip()

    if tracer is not None:
        tracer.emit(
            node="responder.no_results",
            latency_ms=(time.perf_counter() - started) * 1000,
            output={
                "path": "deterministic_no_results",
                "no_llm_call": True,
                "no_results_reason": reason,
                "destination": query.destination,
                "origin": query.origin,
                "max_price_usd": query.max_price_usd,
            },
        )
    return text


__all__ = ["CRITIQUE_PROMPT", "RESPONDER_PROMPT", "SELF_CRITIQUE_ENV", "respond"]
