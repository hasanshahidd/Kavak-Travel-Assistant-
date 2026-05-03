"""Flight search graph node — wraps :class:`FlightIndex` with tracing.

Pure function ``search_flights()`` ready to lift into the LangGraph state
machine in Block 6. Designed to be testable in isolation by passing an
index and (optionally) a tracer; no implicit dependencies on graph state.

The trace event is rich on purpose — when relaxation happens, the user
sees a "we dropped X" note, but the trace records exactly which
constraints were relaxed and the candidate-pool size. That's what lets
the eval suite measure "how often does the bot relax vs. exact-match"
without re-running the whole agent.
"""

from __future__ import annotations

import time

from app.llm.tracing import Tracer
from app.schemas.flight import FlightQuery, SearchOutcome
from app.tools.flight_index import FlightIndex

DEFAULT_TOP_K = 3


def search_flights(
    *,
    query: FlightQuery,
    index: FlightIndex,
    tracer: Tracer | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> SearchOutcome:
    """Run the flight search and emit a trace event describing the outcome."""
    started = time.perf_counter()
    outcome = index.search(query, top_k=top_k)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if tracer is not None:
        tracer.emit(
            node="flight_search",
            latency_ms=elapsed_ms,
            output={
                "query": query.model_dump(mode="json", exclude={"scratchpad"}),
                "result_count": len(outcome.results),
                "total_matched": outcome.total_matched,
                "is_relaxed": outcome.is_relaxed,
                "relaxed_constraints": outcome.relaxed_constraints,
                "no_results_reason": outcome.no_results_reason,
                "top_results": [
                    {
                        "id": r.flight.id,
                        "airline": r.flight.airline,
                        "price_usd": r.flight.price_usd,
                        "score": r.score,
                        "explanation": r.explanation,
                    }
                    for r in outcome.results
                ],
            },
        )

    return outcome


__all__ = ["DEFAULT_TOP_K", "search_flights"]
