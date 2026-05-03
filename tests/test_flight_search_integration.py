"""End-to-end flight-search integration test.

Proves Block 5 wires together with everything before it:

  Block 1 schemas       → ``FlightQuery``, ``SearchOutcome``, ``FlightResult``
  Block 2 substrate     → prompt loader, LLM client, tracer
  Block 3 prompts       → ``flight_responder.md``
  Block 5 prompts       → ``responder_critique.md`` (when self-critique on)
  Block 5 utils         → airports.expand, alliances.alliance_of, dates.matches_month
  Block 5 tools         → ``FlightIndex`` with relaxation
  Block 5 graph nodes   → ``search_flights`` + ``respond``

If any layer breaks, this test fails. Mirrors the structure of
test_rag_integration.py — same proof pattern for the flight side.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.graph.nodes.flight_search import search_flights
from app.graph.nodes.responder import respond
from app.llm.client import MockClient
from app.llm.tracing import Tracer
from app.schemas.flight import FlightQuery
from app.tools.flight_index import FlightIndex


@pytest.fixture
def index() -> FlightIndex:
    return FlightIndex()


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer(turn_id="flight-int-001", trace_dir=tmp_path / "traces", redact=True)


def test_full_flight_path_spec_example(index: FlightIndex, tracer: Tracer) -> None:
    """The spec's canonical query: round-trip Dubai → Tokyo, August, Star Alliance, no overnight."""
    query = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )

    # Step 1: search
    outcome = search_flights(query=query, index=index, tracer=tracer)
    assert outcome.results, "spec query must return matches"
    assert outcome.relaxed_constraints == []  # exact match — no relaxation
    # Every result respects the constraints
    for r in outcome.results:
        assert r.flight.alliance == "Star Alliance"
        assert r.flight.is_overnight_layover is False

    # Step 2: respond (no self-critique, default off)
    canned = (
        "I found 2 Star Alliance round-trip options from Dubai to Tokyo in August "
        "without overnight layovers.\n\n"
        "1. **Turkish Airlines · DXB → NRT** | $950 | 5.5h IST layover\n"
        "2. **Singapore Airlines · DXB → NRT** | $1220 | 4h SIN layover\n\n"
        "Want me to filter by price?"
    )
    client = MockClient(default_text=canned)
    reply = respond(query=query, outcome=outcome, client=client, tracer=tracer)
    assert reply == canned

    # Step 3: trace contains both nodes
    nodes = [e.node for e in tracer.events]
    assert nodes == ["flight_search", "responder.draft"]

    search_event = tracer.events[0]
    assert search_event.output["result_count"] == len(outcome.results)
    assert search_event.output["is_relaxed"] is False
    assert search_event.output["query"]["origin"] == "Dubai"

    responder_event = tracer.events[1]
    assert responder_event.prompt_id == "flight_responder.v2"


def test_relaxation_path_to_paris(index: FlightIndex, tracer: Tracer) -> None:
    """User wants Star Alliance to Paris; only SkyTeam/independent serve CDG.
    Search must relax the alliance and the trace must report it."""
    query = FlightQuery(
        origin="Dubai",
        destination="Paris",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
    )
    outcome = search_flights(query=query, index=index, tracer=tracer)
    assert outcome.results, "relaxed search should find Paris flights"
    assert "preferred_alliances" in outcome.relaxed_constraints

    search_event = tracer.events[0]
    assert search_event.output["is_relaxed"] is True
    assert "preferred_alliances" in search_event.output["relaxed_constraints"]


def test_no_results_path_includes_diagnosis(index: FlightIndex, tracer: Tracer) -> None:
    """Atlantis isn't in the dataset — outcome must include a useful no_results_reason."""
    query = FlightQuery(
        origin="Dubai", destination="Atlantis", departure_date=date(2026, 8, 1)
    )
    outcome = search_flights(query=query, index=index, tracer=tracer)
    assert outcome.results == []
    assert outcome.no_results_reason

    search_event = tracer.events[0]
    assert search_event.output["no_results_reason"] is not None


def test_self_critique_loop_traces_three_events(index: FlightIndex, tracer: Tracer) -> None:
    """When critique flags issues, the trace shows draft + critique + revision."""
    from app.schemas.flight import ResponseCritique

    query = FlightQuery(
        origin="Dubai", destination="Tokyo", departure_date=date(2026, 8, 1)
    )
    outcome = search_flights(query=query, index=index, tracer=tracer)
    assert outcome.results

    fail_critique = ResponseCritique(
        needs_revision=True,
        issues=["Mentions a price not in the flight data."],
        confidence=0.9,
    )
    client = MockClient(default_text="Draft text.")
    client.register(
        "responder_critique.v1",
        raw_text=fail_critique.model_dump_json(),
        parsed=fail_critique,
    )

    respond(query=query, outcome=outcome, client=client, tracer=tracer, self_critique=True)

    nodes = [e.node for e in tracer.events]
    # search + draft + critique + revision = 4 events
    assert nodes == [
        "flight_search",
        "responder.draft",
        "responder.critique",
        "responder.revision",
    ]
