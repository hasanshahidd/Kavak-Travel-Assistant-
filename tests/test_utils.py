"""Utils tests — dates, airports, alliances."""

from __future__ import annotations

from datetime import date

from app.utils.airports import city_for, expand, resolve
from app.utils.alliances import (
    ALLIANCE_BY_AIRLINE,
    airlines_in_alliance,
    alliance_of,
    is_in_alliance,
)
from app.utils.dates import TODAY, matches_month, month_label

# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


def test_today_is_pinned_for_reproducibility() -> None:
    """If this changes, update the extractor prompt's 'Today is...' line in lockstep."""
    assert date(2026, 5, 2) == TODAY


def test_matches_month_same_month_same_year() -> None:
    assert matches_month(date(2026, 8, 15), date(2026, 8, 1)) is True


def test_matches_month_different_month_same_year() -> None:
    assert matches_month(date(2026, 8, 15), date(2026, 9, 1)) is False


def test_matches_month_different_year() -> None:
    assert matches_month(date(2025, 8, 15), date(2026, 8, 1)) is False


def test_matches_month_treats_none_as_no_constraint() -> None:
    assert matches_month(date(2026, 8, 15), None) is True


def test_month_label() -> None:
    assert month_label(date(2026, 8, 15)) == "August 2026"


# ---------------------------------------------------------------------------
# airports
# ---------------------------------------------------------------------------


def test_resolve_iata_passthrough() -> None:
    assert resolve("DXB") == "DXB"
    assert resolve("dxb") == "DXB"


def test_resolve_city_name() -> None:
    assert resolve("Dubai") == "DXB"
    assert resolve("dubai") == "DXB"


def test_resolve_city_alias() -> None:
    assert resolve("Bombay") == "BOM"


def test_resolve_unknown_returns_none() -> None:
    assert resolve("Atlantis") is None
    assert resolve("") is None
    assert resolve(None) is None


def test_expand_returns_all_iatas_for_multi_airport_city() -> None:
    nyc = expand("New York")
    assert {"JFK", "EWR", "LGA"} <= nyc


def test_expand_returns_all_iatas_for_tokyo() -> None:
    tokyo = expand("Tokyo")
    assert {"NRT", "HND"} <= tokyo


def test_expand_single_airport_city() -> None:
    assert expand("Dubai") == {"DXB"}


def test_expand_unknown_returns_empty_set() -> None:
    assert expand("Atlantis") == set()
    assert expand(None) == set()


def test_city_for_iata() -> None:
    assert city_for("DXB") == "Dubai"
    assert city_for("nrt") == "Tokyo"
    assert city_for("UNKNOWN") is None


def test_country_for_city_maps_known_cities() -> None:
    """Used by the retriever to bridge city↔country in the embedding space."""
    from app.utils.airports import country_for_city

    assert country_for_city("Tokyo") == "Japan"
    assert country_for_city("Dubai") == "UAE"
    assert country_for_city("New York") == "USA"
    assert country_for_city("London") == "UK"
    # Already a country → no city to map; expand() finds nothing.
    assert country_for_city("Atlantis") is None


def test_country_name_resolves_to_country_airports() -> None:
    """Country names should expand to every airport in that country.

    Real user phrasing: "flights from UAE to Tokyo" — UAE is the country,
    not a city. The resolver makes that work without forcing the user to
    know which IATA they want.
    """
    assert {"DXB", "AUH"} <= expand("UAE")
    assert {"DXB", "AUH"} <= expand("uae")
    assert {"DXB", "AUH"} <= expand("united arab emirates")
    assert {"NRT", "HND"} <= expand("Japan")
    assert {"JFK", "EWR", "LGA"} <= expand("USA")
    assert {"JFK", "EWR", "LGA"} <= expand("United States")
    assert {"LHR"} <= expand("UK")
    assert {"LHR"} <= expand("United Kingdom")


# ---------------------------------------------------------------------------
# alliances
# ---------------------------------------------------------------------------


def test_alliance_of_known_airline() -> None:
    assert alliance_of("Turkish Airlines") == "Star Alliance"
    assert alliance_of("British Airways") == "OneWorld"
    assert alliance_of("Air France") == "SkyTeam"


def test_alliance_of_independent_carrier() -> None:
    assert alliance_of("Emirates") is None
    assert alliance_of("FlyDubai") is None


def test_alliance_of_unknown_airline() -> None:
    assert alliance_of("Made-Up Airways") is None


def test_airlines_in_alliance() -> None:
    star = airlines_in_alliance("Star Alliance")
    assert {"Lufthansa", "Singapore Airlines", "Turkish Airlines"} <= star


def test_airlines_in_alliance_case_insensitive() -> None:
    assert airlines_in_alliance("STAR ALLIANCE") == airlines_in_alliance("Star Alliance")


def test_is_in_alliance() -> None:
    assert is_in_alliance("Lufthansa", "Star Alliance") is True
    assert is_in_alliance("British Airways", "Star Alliance") is False
    assert is_in_alliance("Emirates", "Star Alliance") is False


def test_every_alliance_in_dataset_is_recognised() -> None:
    """Sanity: every alliance in our flights.json must be in the alliance map."""
    import json

    from app.config import get_settings

    flights = json.loads(get_settings().flights_path.read_text(encoding="utf-8"))
    for f in flights:
        airline = f["airline"]
        if airline in ALLIANCE_BY_AIRLINE:  # mapped
            expected = ALLIANCE_BY_AIRLINE[airline]
            actual_in_data = f.get("alliance")
            assert expected == actual_in_data, (
                f"flight.json says {airline}={actual_in_data}, alliance map says {expected}"
            )
