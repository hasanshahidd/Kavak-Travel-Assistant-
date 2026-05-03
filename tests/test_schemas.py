"""Schema validation tests.

Acceptance gate for Block 1: every flight in flights.json parses cleanly,
the KB docs exist, and the Pydantic invariants we rely on actually hold.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.schemas import (
    Citation,
    Flight,
    FlightQuery,
    FlightResult,
    Intent,
    RagAnswer,
    RouterOutput,
    TripType,
)

# ---------------------------------------------------------------------------
# flights.json — full dataset must parse
# ---------------------------------------------------------------------------


def test_all_flights_parse() -> None:
    settings = get_settings()
    raw = json.loads(settings.flights_path.read_text(encoding="utf-8"))
    assert len(raw) >= 30, f"Expected at least 30 flights, got {len(raw)}"
    flights = [Flight.model_validate(r) for r in raw]
    assert all(isinstance(f, Flight) for f in flights)


def test_flight_dataset_diversity() -> None:
    """Block 1 spec: deliberate variety across alliances, layovers, refundability, price."""
    settings = get_settings()
    flights = [Flight.model_validate(r) for r in json.loads(settings.flights_path.read_text())]

    alliances = {f.alliance for f in flights}
    assert {"Star Alliance", "OneWorld", "SkyTeam", None} <= alliances, (
        f"Missing alliance variety: {alliances}"
    )

    assert any(f.is_overnight_layover for f in flights), "Need overnight-layover flights"
    assert any(f.layover_hours == 0 for f in flights), "Need direct flights"
    assert any(f.refundable for f in flights), "Need refundable flights"
    assert any(not f.refundable for f in flights), "Need non-refundable flights"

    prices = [f.price_usd for f in flights]
    assert min(prices) <= 500, "Need at least one budget option"
    assert max(prices) >= 1500, "Need at least one premium option"


# ---------------------------------------------------------------------------
# Flight invariants
# ---------------------------------------------------------------------------


def test_flight_layover_hours_required_when_layovers_present() -> None:
    with pytest.raises(ValidationError):
        Flight(
            id="X1",
            airline="X",
            alliance=None,
            origin="DXB",
            destination="LHR",
            departure_date=date(2026, 8, 1),
            layovers=["IST"],
            layover_hours=0.0,
            is_overnight_layover=False,
            price_usd=500.0,
            refundable=True,
        )


def test_flight_overnight_requires_layovers() -> None:
    with pytest.raises(ValidationError):
        Flight(
            id="X2",
            airline="X",
            alliance=None,
            origin="DXB",
            destination="LHR",
            departure_date=date(2026, 8, 1),
            layovers=[],
            layover_hours=0.0,
            is_overnight_layover=True,
            price_usd=500.0,
            refundable=True,
        )


def test_flight_iata_uppercased() -> None:
    f = Flight(
        id="X3",
        airline="X",
        alliance=None,
        origin="dxb",
        destination="lhr",
        departure_date=date(2026, 8, 1),
        layovers=["ist"],
        layover_hours=2.0,
        is_overnight_layover=False,
        price_usd=500.0,
        refundable=True,
    )
    assert f.origin == "DXB"
    assert f.destination == "LHR"
    assert f.layovers == ["IST"]


# ---------------------------------------------------------------------------
# FlightQuery
# ---------------------------------------------------------------------------


def test_flight_query_empty_is_valid() -> None:
    q = FlightQuery()
    assert q.trip_type is TripType.ROUND_TRIP
    assert q.preferred_alliances == []
    assert not q.needs_clarification


def test_flight_query_return_before_departure_rejected() -> None:
    with pytest.raises(ValidationError):
        FlightQuery(
            departure_date=date(2026, 8, 30),
            return_date=date(2026, 8, 15),
        )


def test_flight_query_one_way_drops_return_date() -> None:
    q = FlightQuery(
        trip_type=TripType.ONE_WAY,
        departure_date=date(2026, 8, 15),
        return_date=date(2026, 8, 30),
    )
    assert q.return_date is None


def test_flight_query_strips_blank_strings() -> None:
    q = FlightQuery(preferred_alliances=["Star Alliance", "  ", ""])
    assert q.preferred_alliances == ["Star Alliance"]


# ---------------------------------------------------------------------------
# RAG schemas — citation enforcement is the central invariant
# ---------------------------------------------------------------------------


def test_citation_normalizes_doc_name() -> None:
    c = Citation(doc="Visa_Rules.MD", span="UAE passport holders can enter Japan")
    assert c.doc == "visa_rules.md"


def test_citation_rejects_too_short_span() -> None:
    with pytest.raises(ValidationError):
        Citation(doc="visa_rules.md", span="short")


def test_rag_answer_drops_blank_spans() -> None:
    a = RagAnswer(
        answer="UAE passport holders can enter Japan visa-free.",
        citations=[
            Citation(doc="visa_rules.md", span="UAE passport holders can enter Japan"),
            Citation(doc="visa_rules.md", span="              "),
        ],
        confidence=0.9,
    )
    assert len(a.citations) == 1


def test_rag_answer_refusal_can_omit_citations() -> None:
    a = RagAnswer(
        answer="I don't have information about Atlantis visa requirements.",
        citations=[],
        is_refusal=True,
    )
    assert a.is_refusal is True


# ---------------------------------------------------------------------------
# Intent + RouterOutput
# ---------------------------------------------------------------------------


def test_intent_enum_values() -> None:
    assert Intent.FLIGHT_SEARCH == "flight_search"
    assert Intent.POLICY_QA == "policy_qa"
    assert Intent.CLARIFY == "clarify"
    assert Intent.OUT_OF_SCOPE == "out_of_scope"


def test_router_output_requires_rationale() -> None:
    out = RouterOutput(intent=Intent.FLIGHT_SEARCH, rationale="user wants flights")
    assert out.intent is Intent.FLIGHT_SEARCH


# ---------------------------------------------------------------------------
# FlightResult
# ---------------------------------------------------------------------------


def test_flight_result_carries_relaxation_info() -> None:
    f = Flight(
        id="X4",
        airline="EK",
        alliance=None,
        origin="DXB",
        destination="NRT",
        departure_date=date(2026, 8, 15),
        layovers=[],
        layover_hours=0.0,
        is_overnight_layover=False,
        price_usd=1180.0,
        refundable=True,
    )
    r = FlightResult(
        flight=f,
        score=0.42,
        explanation="Direct, refundable",
        relaxed_constraints=["preferred_alliance"],
    )
    assert r.relaxed_constraints == ["preferred_alliance"]


# ---------------------------------------------------------------------------
# KB documents must exist (sanity)
# ---------------------------------------------------------------------------


def test_kb_documents_present() -> None:
    settings = get_settings()
    expected = ["visa_rules.md", "refund_policy.md", "baggage_policy.md"]
    for name in expected:
        path = settings.kb_dir / name
        assert path.exists(), f"Missing KB doc: {path}"
        assert path.stat().st_size > 200, f"KB doc too short: {path}"


def test_airports_json_parses() -> None:
    settings = get_settings()
    raw = json.loads(settings.airports_path.read_text(encoding="utf-8"))
    assert "DXB" in raw
    assert "aliases" in raw["DXB"]
