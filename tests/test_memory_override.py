"""Memory override semantics — the D4 differentiator.

Most chat agents fail multi-turn refinement in two ways:
* **State leak** — old filters bleed into a new search
* **State amnesia** — every turn starts fresh, losing destination/dates

These tests prove our merge logic handles the five scenarios that matter:

1. First turn (no prior state) → use the new query as-is.
2. Single-field override ("actually move it to September") → keep
   destination, alliance, no-overnight; replace date only.
3. Topic switch ("now show me Paris") → reset all soft preferences,
   keep origin (stable user property).
4. Refinement with empty fields ("make it cheaper") — extractor only
   sets ``max_price_usd``; merge fills destination/date from prior.
5. Same-city alias resolution — "Tokyo" and "NRT" must NOT trigger a
   topic switch (they refer to the same destination).
"""

from __future__ import annotations

from datetime import date

from app.memory.conversation import (
    Conversation,
    is_topic_switch,
    merge_query,
)
from app.schemas.flight import FlightQuery, TripType

# ---------------------------------------------------------------------------
# Scenario 1 — first turn, no prior state
# ---------------------------------------------------------------------------


def test_first_turn_uses_new_query_as_is() -> None:
    new = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    merged = merge_query(prior=None, new=new)
    assert merged == new


# ---------------------------------------------------------------------------
# Scenario 2 — date-only override preserves all other state
# ---------------------------------------------------------------------------


def test_date_override_preserves_other_filters() -> None:
    """User: 'actually move it to September' — extractor returns full query
    with date changed; merge should not destroy alliance/no-overnight."""
    prior = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        trip_type=TripType.ROUND_TRIP,
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    new = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 9, 1),
        trip_type=TripType.ROUND_TRIP,
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    merged = merge_query(prior=prior, new=new)
    assert merged.departure_date == date(2026, 9, 1)
    assert merged.preferred_alliances == ["Star Alliance"]
    assert merged.avoid_overnight_layovers is True


def test_date_override_when_extractor_omits_unchanged_fields() -> None:
    """Real-world case: extractor only emits the field that changed.
    Merge should fill the rest from prior."""
    prior = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    # Sparse extractor output — only the changed field.
    new = FlightQuery(
        departure_date=date(2026, 9, 1),
    )
    merged = merge_query(prior=prior, new=new)
    assert merged.departure_date == date(2026, 9, 1)
    assert merged.origin == "Dubai"  # inherited
    assert merged.destination == "Tokyo"  # inherited
    assert merged.preferred_alliances == ["Star Alliance"]  # inherited
    assert merged.avoid_overnight_layovers is True  # inherited


# ---------------------------------------------------------------------------
# Scenario 3 — topic switch resets soft state
# ---------------------------------------------------------------------------


def test_topic_switch_resets_soft_preferences() -> None:
    """User: 'now show me flights to Paris' — Star Alliance + no-overnight
    must NOT carry over to the new search."""
    prior = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    new = FlightQuery(
        origin="Dubai",
        destination="Paris",
    )
    merged = merge_query(prior=prior, new=new)
    assert merged.destination == "Paris"
    assert merged.preferred_alliances == []
    assert merged.avoid_overnight_layovers is False
    assert merged.departure_date is None


def test_is_topic_switch_returns_true_for_different_cities() -> None:
    prior = FlightQuery(origin="Dubai", destination="Tokyo")
    new = FlightQuery(origin="Dubai", destination="Paris")
    assert is_topic_switch(prior, new) is True


def test_is_topic_switch_returns_false_when_destination_unchanged() -> None:
    prior = FlightQuery(origin="Dubai", destination="Tokyo")
    new = FlightQuery(origin="Dubai", destination="Tokyo")
    assert is_topic_switch(prior, new) is False


def test_is_topic_switch_false_when_new_destination_omitted() -> None:
    """User saying only 'make it cheaper' should NOT be a topic switch."""
    prior = FlightQuery(origin="Dubai", destination="Tokyo")
    new = FlightQuery()  # extractor emitted nothing about destination
    assert is_topic_switch(prior, new) is False


# ---------------------------------------------------------------------------
# Scenario 4 — refinement with sparse new query inherits prior context
# ---------------------------------------------------------------------------


def test_make_it_cheaper_inherits_destination_and_date() -> None:
    prior = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
    )
    new = FlightQuery(max_price_usd=800)  # only the new constraint
    merged = merge_query(prior=prior, new=new)
    assert merged.max_price_usd == 800
    assert merged.destination == "Tokyo"
    assert merged.departure_date == date(2026, 8, 1)
    assert merged.preferred_alliances == ["Star Alliance"]


# ---------------------------------------------------------------------------
# Scenario 5 — IATA / city-name aliases must not trigger topic switch
# ---------------------------------------------------------------------------


def test_iata_alias_does_not_trigger_topic_switch() -> None:
    """'NRT' and 'Tokyo' refer to the same destination."""
    prior = FlightQuery(origin="DXB", destination="Tokyo")
    new = FlightQuery(origin="DXB", destination="NRT", departure_date=date(2026, 9, 1))
    assert is_topic_switch(prior, new) is False
    merged = merge_query(prior=prior, new=new)
    # No reset — the destination effectively didn't change
    assert merged.departure_date == date(2026, 9, 1)


def test_multi_airport_city_alias_does_not_trigger_switch() -> None:
    """'New York' and 'JFK' should be treated as the same destination."""
    prior = FlightQuery(origin="DXB", destination="New York")
    new = FlightQuery(origin="DXB", destination="JFK")
    assert is_topic_switch(prior, new) is False


# ---------------------------------------------------------------------------
# Conversation store — store + recall + summary
# ---------------------------------------------------------------------------


def test_conversation_stores_and_recalls_query() -> None:
    convo = Conversation()
    assert convo.prior_query is None

    q1 = FlightQuery(origin="Dubai", destination="Tokyo", departure_date=date(2026, 8, 1))
    convo.commit_query(q1)
    assert convo.prior_query == q1


def test_conversation_summary_includes_committed_query() -> None:
    convo = Conversation()
    convo.commit_query(
        FlightQuery(
            origin="Dubai",
            destination="Tokyo",
            departure_date=date(2026, 8, 1),
            preferred_alliances=["Star Alliance"],
        )
    )
    s = convo.summary()
    assert "Dubai" in s
    assert "Tokyo" in s
    assert "Star Alliance" in s
    assert "Aug 2026" in s


def test_conversation_summary_when_empty() -> None:
    convo = Conversation()
    assert convo.summary() == "(no prior conversation)"


def test_conversation_message_window() -> None:
    convo = Conversation(window=3)
    for i in range(5):
        convo.add_user_message(f"turn {i}")
    windowed = convo.windowed_messages()
    assert len(windowed) == 3
    assert "turn 4" in windowed[-1].content


def test_conversation_reset_clears_everything() -> None:
    convo = Conversation()
    convo.add_user_message("hi")
    convo.commit_query(FlightQuery(origin="Dubai", destination="Tokyo"))
    convo.reset()
    assert convo.prior_query is None
    assert convo.messages == []
    assert convo.summary() == "(no prior conversation)"


# ---------------------------------------------------------------------------
# Edge case — nothing inheritable, merge is a no-op
# ---------------------------------------------------------------------------


def test_no_inheritance_when_new_fully_specified() -> None:
    prior = FlightQuery(origin="Dubai", destination="Tokyo", max_price_usd=1000)
    new = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        max_price_usd=500,
    )
    merged = merge_query(prior=prior, new=new)
    # New explicit value (500) wins, not the prior 1000.
    assert merged.max_price_usd == 500
    assert merged.departure_date == date(2026, 8, 1)
