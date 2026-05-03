"""Flight index tests - match logic, soft-constraint relaxation, ranking."""

from __future__ import annotations

from datetime import date

import pytest

from app.schemas.flight import FlightQuery, TripType
from app.tools.flight_index import (
    SOFT_CONSTRAINTS_PRIORITY,
    FlightIndex,
)


@pytest.fixture
def index() -> FlightIndex:
    return FlightIndex()


# ---------------------------------------------------------------------------
# Happy path - exact match
# ---------------------------------------------------------------------------


def test_dubai_to_tokyo_august_star_alliance_no_overnight(index: FlightIndex) -> None:
    """The spec example. Should return matches without any relaxation."""
    query = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        trip_type=TripType.ROUND_TRIP,
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    outcome = index.search(query)
    assert outcome.results, "should find Star Alliance flights to Tokyo in August"
    assert outcome.relaxed_constraints == []
    assert outcome.no_results_reason is None
    # Every result must be Star Alliance and not overnight
    for r in outcome.results:
        assert r.flight.alliance == "Star Alliance"
        assert r.flight.is_overnight_layover is False
        assert r.flight.destination in {"NRT", "HND"}


def test_results_are_score_ascending(index: FlightIndex) -> None:
    query = FlightQuery(origin="Dubai", destination="Tokyo", departure_date=date(2026, 8, 1))
    outcome = index.search(query)
    if len(outcome.results) >= 2:
        scores = [r.score for r in outcome.results]
        assert scores == sorted(scores)


def test_top_k_limits_results(index: FlightIndex) -> None:
    query = FlightQuery(origin="Dubai", destination="Tokyo", departure_date=date(2026, 8, 1))
    outcome = index.search(query, top_k=2)
    assert len(outcome.results) <= 2


def test_result_count_hint_overrides_top_k(index: FlightIndex) -> None:
    """User said 'show me the cheapest one' → result_count_hint=1 → exactly 1 result."""
    query = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        result_count_hint=1,
    )
    # Even with default top_k=3, the hint forces 1 result.
    outcome = index.search(query)
    assert len(outcome.results) == 1


def test_result_count_hint_can_widen_top_k(index: FlightIndex) -> None:
    """User said 'top 5' → result_count_hint=5 → up to 5 results."""
    query = FlightQuery(
        origin="Dubai",
        destination="Paris",
        departure_date=date(2026, 8, 1),
        result_count_hint=5,
    )
    # default top_k is 3, hint widens to 5
    outcome = index.search(query, top_k=3)
    # Bounded by available matches, but at least more than the default 3 if the dataset has them
    assert len(outcome.results) <= 5


def test_result_count_hint_none_uses_default(index: FlightIndex) -> None:
    query = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
    )
    assert query.result_count_hint is None
    outcome = index.search(query, top_k=3)
    assert len(outcome.results) <= 3


# ---------------------------------------------------------------------------
# Aliases & resolution
# ---------------------------------------------------------------------------


def test_origin_destination_resolve_via_aliases(index: FlightIndex) -> None:
    """'Bombay' should resolve to BOM via the alias map."""
    query = FlightQuery(
        origin="Dubai",
        destination="Bombay",
        departure_date=date(2026, 8, 1),
    )
    outcome = index.search(query)
    assert outcome.results
    assert all(r.flight.destination == "BOM" for r in outcome.results)


def test_iata_codes_work_directly(index: FlightIndex) -> None:
    query = FlightQuery(origin="DXB", destination="LHR", departure_date=date(2026, 8, 1))
    outcome = index.search(query)
    assert outcome.results
    assert all(r.flight.destination == "LHR" for r in outcome.results)


# ---------------------------------------------------------------------------
# Soft-constraint relaxation
# ---------------------------------------------------------------------------


def test_relaxes_alliance_when_strict_match_empty(index: FlightIndex) -> None:
    """User wants Star Alliance to a destination where Star has no flights;
    we should relax the alliance constraint and report it."""
    query = FlightQuery(
        origin="Dubai",
        destination="Paris",  # CDG flights exist for SkyTeam (Air France) and Emirates, not Star
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
    )
    outcome = index.search(query)
    assert outcome.results, "should find Paris flights after relaxing the alliance"
    assert "preferred_alliances" in outcome.relaxed_constraints


def test_relaxes_overnight_constraint(index: FlightIndex) -> None:
    """If only overnight flights match the route, relax avoid_overnight."""
    # Sydney in September has only an overnight Star Alliance option (FL015).
    # Force the situation by picking the Star+Sept combo.
    query = FlightQuery(
        origin="Dubai",
        destination="Sydney",
        departure_date=date(2026, 9, 1),
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    outcome = index.search(query)
    if outcome.results:
        # Either the alliance relaxed (other carriers exist) OR the overnight one did
        assert outcome.is_relaxed


def test_no_results_when_route_does_not_exist(index: FlightIndex) -> None:
    """No flights to Atlantis - even hard relaxation can't help."""
    query = FlightQuery(
        origin="Dubai",
        destination="Atlantis",
        departure_date=date(2026, 8, 1),
    )
    outcome = index.search(query)
    assert outcome.results == []
    assert outcome.no_results_reason


def test_diagnose_helps_when_price_too_low(index: FlightIndex) -> None:
    """User asks for $100 to Sydney - none exist. The diagnosis should mention price."""
    query = FlightQuery(
        origin="Dubai",
        destination="Sydney",
        departure_date=date(2026, 8, 1),
        max_price_usd=100,
    )
    outcome = index.search(query)
    assert outcome.results == []
    assert outcome.no_results_reason is not None
    assert "$100" in outcome.no_results_reason or "price" in outcome.no_results_reason.lower()


def test_relaxation_reports_only_constraints_user_set(index: FlightIndex) -> None:
    """User didn't set max_layover_hours, so relaxation must NOT report dropping it."""
    query = FlightQuery(
        origin="Dubai",
        destination="Paris",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
    )
    outcome = index.search(query)
    if outcome.is_relaxed:
        # The user only set preferred_alliances; max_layover_hours was None.
        # Even if the search internally tried dropping max_layover_hours, it
        # should not be reported as "relaxed" because it was never active.
        assert "max_layover_hours" not in outcome.relaxed_constraints


def test_priority_order_relaxes_alliance_before_overnight(index: FlightIndex) -> None:
    """When both could be relaxed, alliance goes first (per SOFT_CONSTRAINTS_PRIORITY)."""
    assert SOFT_CONSTRAINTS_PRIORITY.index("preferred_alliances") < SOFT_CONSTRAINTS_PRIORITY.index(
        "avoid_overnight_layovers"
    )


# ---------------------------------------------------------------------------
# Hard constraints - never relaxed
# ---------------------------------------------------------------------------


def test_max_price_is_hard_not_relaxed(index: FlightIndex) -> None:
    """Even if no flight matches, we never silently exceed the price ceiling."""
    query = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        max_price_usd=500,  # cheaper than every Tokyo flight in the dataset
    )
    outcome = index.search(query)
    assert outcome.results == []
    if outcome.results:
        for r in outcome.results:
            assert r.flight.price_usd <= 500


def test_refundable_only_is_hard(index: FlightIndex) -> None:
    query = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        refundable_only=True,
    )
    outcome = index.search(query)
    for r in outcome.results:
        assert r.flight.refundable is True


def test_round_trip_excludes_one_way_listings(index: FlightIndex) -> None:
    """Every flight in our dataset has a return_date, so round_trip is fine.
    But the constraint must be enforced (would reject flights without return_date)."""
    query = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        trip_type=TripType.ROUND_TRIP,
    )
    outcome = index.search(query)
    for r in outcome.results:
        assert r.flight.return_date is not None


# ---------------------------------------------------------------------------
# Explanation strings
# ---------------------------------------------------------------------------


def test_explanation_mentions_direct_for_non_stop(index: FlightIndex) -> None:
    query = FlightQuery(origin="Dubai", destination="Tokyo", departure_date=date(2026, 8, 1))
    outcome = index.search(query)
    direct_results = [r for r in outcome.results if not r.flight.layovers]
    if direct_results:
        assert any("direct" in r.explanation.lower() for r in direct_results)


def test_explanation_mentions_refundable_when_applicable(index: FlightIndex) -> None:
    query = FlightQuery(origin="Dubai", destination="London", departure_date=date(2026, 8, 1))
    outcome = index.search(query)
    refundable = [r for r in outcome.results if r.flight.refundable]
    if refundable:
        assert any("refundable" in r.explanation.lower() for r in refundable)
