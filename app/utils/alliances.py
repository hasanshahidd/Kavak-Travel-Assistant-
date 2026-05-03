"""Airline ↔ alliance map.

The extractor receives natural-language airline / alliance preferences and
sets ``preferred_airlines`` / ``preferred_alliances`` on the FlightQuery.
The flight index needs to translate between them - for example, a query
that asked for "Star Alliance" should match any Star Alliance airline in
the dataset, not require the dataset to also mention "Star Alliance".

This map is the single source of truth for that translation. Kept in code
(not data) because the relationship is stable and small enough that a
review-friendly Python literal beats yet another JSON file.
"""

from __future__ import annotations

# Source of truth. Keep alphabetised within each alliance for review ergonomics.
ALLIANCE_BY_AIRLINE: dict[str, str | None] = {
    # Star Alliance
    "Air Canada": "Star Alliance",
    "Air India": "Star Alliance",
    "ANA": "Star Alliance",
    "Lufthansa": "Star Alliance",
    "Singapore Airlines": "Star Alliance",
    "Thai Airways": "Star Alliance",
    "Turkish Airlines": "Star Alliance",
    "United Airlines": "Star Alliance",
    # OneWorld
    "British Airways": "OneWorld",
    "Cathay Pacific": "OneWorld",
    "JAL": "OneWorld",
    "Malaysia Airlines": "OneWorld",
    "Qantas": "OneWorld",
    "Qatar Airways": "OneWorld",
    # SkyTeam
    "Air France": "SkyTeam",
    "China Eastern": "SkyTeam",
    "Delta": "SkyTeam",
    "Korean Air": "SkyTeam",
    "KLM": "SkyTeam",
    "Saudia": "SkyTeam",
    # Independent
    "Emirates": None,
    "Etihad Airways": None,
    "FlyDubai": None,
    "IndiGo": None,
}


def alliance_of(airline: str) -> str | None:
    """Return the alliance for ``airline``, or None if unknown / independent."""
    return ALLIANCE_BY_AIRLINE.get(airline)


def airlines_in_alliance(alliance: str) -> set[str]:
    """All airlines we know about that belong to ``alliance``."""
    target = alliance.strip().lower()
    return {
        airline
        for airline, a in ALLIANCE_BY_AIRLINE.items()
        if a is not None and a.lower() == target
    }


def is_in_alliance(airline: str, alliance: str) -> bool:
    """Convenience: True iff ``airline`` belongs to ``alliance`` (case-insensitive)."""
    actual = alliance_of(airline)
    return actual is not None and actual.lower() == alliance.strip().lower()


__all__ = [
    "ALLIANCE_BY_AIRLINE",
    "airlines_in_alliance",
    "alliance_of",
    "is_in_alliance",
]
