"""Airport / city alias resolver, backed by ``data/airports.json``.

Two queries the rest of the codebase needs:

1. **resolve(text) -> IATA | None** — "Tokyo" → "NRT", "bombay" → "BOM",
   already-an-IATA "DXB" → "DXB". Used when the user gives a city name and
   we need a single canonical airport for ranking.

2. **expand(text) -> set[IATA]** — "New York" → {"JFK", "EWR", "LGA"},
   "Tokyo" → {"NRT", "HND"}. Used by the flight matcher so a search for
   "to New York" matches flights to any NYC airport.

The resolver is case-insensitive, punctuation-tolerant, and falls back to
the input unchanged if it already looks like an IATA code (3 uppercase
letters). Anything truly unknown returns ``None`` from ``resolve`` and an
empty set from ``expand`` — callers can decide how to handle that.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from app.config import get_settings

_IATA_PATTERN = re.compile(r"^[A-Z]{3}$")

# Country-name synonyms so common phrasings resolve to the country's airports.
# Keep small and conservative — every entry needs a real-world reason to exist.
# Maps free-text → the canonical `country` value used in airports.json.
_COUNTRY_SYNONYMS: dict[str, str] = {
    "uae": "UAE",
    "u.a.e.": "UAE",
    "u.a.e": "UAE",
    "united arab emirates": "UAE",
    "emirates": "UAE",
    "uk": "UK",
    "u.k.": "UK",
    "england": "UK",
    "britain": "UK",
    "great britain": "UK",
    "united kingdom": "UK",
    "usa": "USA",
    "u.s.a.": "USA",
    "u.s.": "USA",
    "us": "USA",
    "united states": "USA",
    "united states of america": "USA",
    "america": "USA",
}


@lru_cache(maxsize=1)
def _load_index() -> tuple[dict[str, dict[str, object]], dict[str, set[str]]]:
    """Load airports.json once. Returns (by_iata, by_alias_lower→{iata}).

    Aliases are pulled from four places per row:
      1. The IATA code itself ("DXB" → DXB)
      2. The explicit ``aliases`` list ("dubai", "dxb")
      3. The ``city`` field ("Dubai" → DXB)
      4. The ``country`` field ("UAE" → DXB and AUH)

    Country-level aliasing makes "flights from UAE to Tokyo" resolve to
    DXB/AUH automatically — country is real data already present, the
    user just used a country name instead of a city. Plus the synonym
    map above handles common phrasings like "United Arab Emirates".
    """
    settings = get_settings()
    raw = json.loads(settings.airports_path.read_text(encoding="utf-8"))

    by_iata: dict[str, dict[str, object]] = {}
    by_alias: dict[str, set[str]] = {}

    # Reverse the synonym map for forward lookup: country → list of synonyms
    country_to_synonyms: dict[str, list[str]] = {}
    for synonym, country in _COUNTRY_SYNONYMS.items():
        country_to_synonyms.setdefault(country, []).append(synonym)

    for iata, info in raw.items():
        iata_upper = iata.upper()
        by_iata[iata_upper] = info
        # Index every alias including the IATA itself
        for alias in {iata_upper.lower(), *info.get("aliases", [])}:  # type: ignore[misc]
            by_alias.setdefault(alias.lower(), set()).add(iata_upper)
        # Also key by city name (lowercased)
        city = str(info.get("city", "")).lower()
        if city:
            by_alias.setdefault(city, set()).add(iata_upper)
        # And by country name + its synonyms — so "UAE" / "United Arab
        # Emirates" both resolve to {DXB, AUH}.
        country = str(info.get("country", "")).strip()
        if country:
            by_alias.setdefault(country.lower(), set()).add(iata_upper)
            for synonym in country_to_synonyms.get(country, []):
                by_alias.setdefault(synonym, set()).add(iata_upper)

    return by_iata, by_alias


def _norm(text: str) -> str:
    return text.strip().lower()


def resolve(text: str | None) -> str | None:
    """Best-effort single IATA. Returns the **first** matching IATA when the
    name resolves to multiple (e.g. NYC → JFK). Use ``expand`` when the
    caller needs all of them.
    """
    if not text:
        return None
    cleaned = text.strip()
    if _IATA_PATTERN.match(cleaned.upper()):
        # Already an IATA. Validate it exists; otherwise fall through to alias lookup.
        by_iata, _ = _load_index()
        if cleaned.upper() in by_iata:
            return cleaned.upper()
    _, by_alias = _load_index()
    matches = by_alias.get(_norm(cleaned))
    if not matches:
        return None
    # Stable ordering so demos are reproducible.
    return sorted(matches)[0]


def expand(text: str | None) -> set[str]:
    """Every IATA the input could mean. Empty set when nothing resolves."""
    if not text:
        return set()
    cleaned = text.strip()
    by_iata, by_alias = _load_index()
    if _IATA_PATTERN.match(cleaned.upper()) and cleaned.upper() in by_iata:
        return {cleaned.upper()}
    return set(by_alias.get(_norm(cleaned), set()))


def city_for(iata: str) -> str | None:
    """Reverse lookup: 'NRT' → 'Tokyo'. None if IATA is unknown."""
    by_iata, _ = _load_index()
    info = by_iata.get(iata.upper())
    if info is None:
        return None
    city = info.get("city")
    return str(city) if city else None


def country_for_city(text: str) -> str | None:
    """Map a city name (or IATA) to its country. ``Tokyo`` → ``Japan``.

    Returns ``None`` if the input doesn't resolve. Used by the RAG
    retriever to bridge the city/country gap in the embedding space —
    the KB talks about *Japan*, the user types *Tokyo*; this helps the
    similarity score clear the relevance threshold.
    """
    iatas = expand(text)
    if not iatas:
        return None
    by_iata, _ = _load_index()
    countries = {str(by_iata[i].get("country", "")).strip() for i in iatas}
    countries.discard("")
    if not countries:
        return None
    # Stable: pick the alphabetically first to match how resolve() picks IATAs.
    return sorted(countries)[0]


__all__ = ["city_for", "country_for_city", "expand", "resolve"]
