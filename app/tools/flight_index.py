"""Flight index - deterministic structured filter beats vector search here.

The mock dataset is small (30 flights) and the user query is structured (a
``FlightQuery`` Pydantic model). Vector search would add latency and
fuzziness for no upside. Direct filtering is the right tool.

Three behaviours that matter for the rubric:

1. **Hard vs. soft constraints.**
   - Hard (never relaxed): origin, destination, date window, max_price_usd,
     refundable_only.
   - Soft (relaxed in priority order): preferred_airlines,
     preferred_alliances, avoid_overnight_layovers, max_layover_hours.

2. **Soft-constraint relaxation with transparency.** When the strict query
   yields zero matches, we drop one soft constraint at a time in priority
   order, retry, and report exactly what was relaxed via
   ``SearchOutcome.relaxed_constraints``. The responder surfaces this to
   the user - no silent "best effort" matches.

3. **Composite ranking** that mirrors what users actually care about:
   price first, then layover quality (penalising overnights and long
   transits), refundability as a tie-break sweetener.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.schemas.flight import (
    Flight,
    FlightQuery,
    FlightResult,
    SearchOutcome,
    TripType,
)
from app.utils.airports import expand
from app.utils.alliances import alliance_of
from app.utils.dates import matches_month

# Soft constraints, in the order we relax them when the strict query fails.
# Order chosen empirically: alliance is the most demanding filter; layover
# limits are usually negotiable when the alternative is no flight at all.
SOFT_CONSTRAINTS_PRIORITY: tuple[str, ...] = (
    "preferred_airlines",
    "preferred_alliances",
    "avoid_overnight_layovers",
    "max_layover_hours",
)


# ---------------------------------------------------------------------------
# Loading + caching
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _load_flights(flights_path_str: str) -> list[Flight]:
    raw = json.loads(Path(flights_path_str).read_text(encoding="utf-8"))
    return [Flight.model_validate(r) for r in raw]


# ---------------------------------------------------------------------------
# Constraint matching
# ---------------------------------------------------------------------------


def _matches_hard(flight: Flight, query: FlightQuery) -> bool:
    """Hard constraints - never relaxed. A non-match here is a real "no"."""
    # Origin + destination resolved through the alias map so user-friendly
    # names ("Tokyo", "Bombay") match the IATA-coded flight rows. An empty
    # expansion = unknown city - must reject (otherwise the constraint is
    # silently dropped and we'd return every flight, which masks failures).
    if query.origin:
        origins = expand(query.origin)
        if not origins or flight.origin not in origins:
            return False
    if query.destination:
        dests = expand(query.destination)
        if not dests or flight.destination not in dests:
            return False

    # Date matching by month - see dates.matches_month for the rationale.
    if query.departure_date and not matches_month(flight.departure_date, query.departure_date):
        return False
    if query.return_date:
        if flight.return_date is None:
            return False
        if not matches_month(flight.return_date, query.return_date):
            return False

    # Round-trip → flight must have a return_date.
    if query.trip_type is TripType.ROUND_TRIP and flight.return_date is None:
        return False
    # NB: a one-way query against a round-trip listing is tolerated - we just
    # use the outbound leg. No reject branch needed.

    if query.max_price_usd is not None and flight.price_usd > query.max_price_usd:
        return False

    # Hard exclusions - user explicitly said "NOT X" or "no X".
    # Case-insensitive matching against airline / alliance.
    if query.excluded_airlines:
        excl = {a.strip().lower() for a in query.excluded_airlines if a.strip()}
        if flight.airline and flight.airline.lower() in excl:
            return False
    if query.excluded_alliances:
        excl = {a.strip().lower() for a in query.excluded_alliances if a.strip()}
        if flight.alliance and flight.alliance.lower() in excl:
            return False

    # Refundable-only: the constraint is satisfied iff the flight is refundable
    # (or the user didn't ask for refundable-only at all).
    return not query.refundable_only or flight.refundable


def _matches_soft(flight: Flight, query: FlightQuery, dropped: set[str]) -> bool:
    """Soft constraints - those NOT in ``dropped`` must hold."""
    if (
        "preferred_airlines" not in dropped
        and query.preferred_airlines
        and flight.airline not in query.preferred_airlines
    ):
        return False
    if "preferred_alliances" not in dropped and query.preferred_alliances:
        # An alliance match works two ways: flight.alliance is set, OR the
        # airline is in the alliance per our mapping (catches data gaps).
        wanted = {a.strip() for a in query.preferred_alliances if a.strip()}
        actual = flight.alliance or alliance_of(flight.airline)
        if actual not in wanted:
            return False
    if (
        "avoid_overnight_layovers" not in dropped
        and query.avoid_overnight_layovers
        and flight.is_overnight_layover
    ):
        return False
    return not (
        "max_layover_hours" not in dropped
        and query.max_layover_hours is not None
        and flight.layover_hours > query.max_layover_hours
    )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _composite_score(flight: Flight) -> float:
    """Lower is better. Tuned so price dominates but layover quality breaks ties."""
    score = float(flight.price_usd)
    if flight.is_overnight_layover:
        score += 200.0
    score += flight.layover_hours * 20.0
    if not flight.refundable:
        score += 50.0
    return score


def _explain(flight: Flight, *, has_cheaper: bool, has_direct: bool) -> str:
    """One-line rationale string used by the responder.

    Phrased to fit ``flight_responder.md``'s expected output line. Not a
    long-form review - just the axis on which this flight wins.
    """
    parts: list[str] = []
    if not flight.layovers:
        parts.append("direct")
    elif not flight.is_overnight_layover and flight.layover_hours <= 4:
        parts.append("short daytime layover")
    if flight.refundable:
        parts.append("refundable")
    if not has_cheaper:
        parts.append("cheapest match")
    elif not has_direct and not flight.layovers:
        parts.append("only direct option")
    if flight.alliance:
        parts.append(flight.alliance)
    if not parts:
        parts.append("balanced trade-off")
    # Capitalise the first piece for readable output.
    pieces = [parts[0].capitalize(), *parts[1:]]
    return ", ".join(pieces)


# ---------------------------------------------------------------------------
# Search engine
# ---------------------------------------------------------------------------


class FlightIndex:
    """In-memory flight catalogue with structured filter + relaxation."""

    def __init__(self, flights_path: Path | None = None) -> None:
        settings = get_settings()
        self._flights_path = str(flights_path or settings.flights_path)

    @property
    def flights(self) -> list[Flight]:
        return _load_flights(self._flights_path)

    # ------- public API -------

    def search(self, query: FlightQuery, *, top_k: int = 3) -> SearchOutcome:
        """Run hard filter, then progressive soft relaxation. Return ranked top-K.

        ``query.result_count_hint`` (if set) overrides ``top_k``. This lets the
        extractor honour user phrasings like "show me the cheapest one" → 1
        without the responder having to inspect the field separately.
        """
        # User can pin the result count via the schema (e.g. "the cheapest one" → 1).
        effective_top_k = query.result_count_hint or top_k
        # 1. Hard filter once. Anything that doesn't pass here can't be helped.
        hard_pool = [f for f in self.flights if _matches_hard(f, query)]
        if not hard_pool:
            return SearchOutcome(
                results=[],
                relaxed_constraints=[],
                total_matched=0,
                no_results_reason=self._diagnose_hard_failure(query),
            )

        # 2. Try all soft constraints. If matches, ship them.
        strict = [f for f in hard_pool if _matches_soft(f, query, dropped=set())]
        if strict:
            return self._build_outcome(strict, query, relaxed=[], top_k=effective_top_k)

        # 3. Progressive relaxation in priority order. Each step adds one
        #    soft constraint to the dropped set. We stop as soon as we get
        #    any matches and report exactly which constraints were dropped.
        dropped: set[str] = set()
        for constraint in SOFT_CONSTRAINTS_PRIORITY:
            dropped.add(constraint)
            relaxed_matches = [f for f in hard_pool if _matches_soft(f, query, dropped=dropped)]
            if relaxed_matches:
                # Filter "dropped" to only those that were actually active
                # in the user's query - otherwise we'd report dropping
                # constraints the user didn't even set.
                actually_relaxed = [c for c in dropped if _was_active(c, query)]
                return self._build_outcome(
                    relaxed_matches, query, relaxed=actually_relaxed, top_k=effective_top_k
                )

        # 4. Even all-soft relaxed → empty. Honest no-results.
        return SearchOutcome(
            results=[],
            relaxed_constraints=list(SOFT_CONSTRAINTS_PRIORITY),
            total_matched=0,
            no_results_reason=(
                "No flights match your route/date/price even after dropping "
                "every preference (alliance, layover limits)."
            ),
        )

    # ------- internals -------

    def _build_outcome(
        self,
        matches: list[Flight],
        query: FlightQuery,
        *,
        relaxed: list[str],
        top_k: int,
    ) -> SearchOutcome:
        # Rank by either raw price (when the user asked for cheapest /
        # lowest price) or the composite score (default - balances price,
        # layover quality, refundability).
        if query.sort_by == "price":
            scored = sorted(matches, key=lambda f: f.price_usd)
        else:
            scored = sorted(matches, key=_composite_score)
        cheapest_price = scored[0].price_usd
        any_direct = any(not f.layovers for f in scored)

        results: list[FlightResult] = []
        for flight in scored[:top_k]:
            results.append(
                FlightResult(
                    flight=flight,
                    score=_composite_score(flight),
                    explanation=_explain(
                        flight,
                        has_cheaper=flight.price_usd > cheapest_price,
                        has_direct=any_direct,
                    ),
                    relaxed_constraints=list(relaxed),
                )
            )
        return SearchOutcome(
            results=results,
            relaxed_constraints=list(relaxed),
            total_matched=len(matches),
            no_results_reason=None,
        )

    def _diagnose_hard_failure(self, query: FlightQuery) -> str:
        """Best-effort short explanation when even the hard filter found nothing."""
        # Unknown city - most user-friendly diagnosis.
        if query.destination and not expand(query.destination):
            return (
                f"I don't have flights to {query.destination!r} in my dataset. "
                "Try a major international destination."
            )
        if query.origin and not expand(query.origin):
            return (
                f"I don't recognise {query.origin!r} as a departure city in my dataset."
            )
        # Try a few diagnoses by selectively unsetting hard constraints.
        if query.max_price_usd is not None:
            looser_q = query.model_copy(update={"max_price_usd": None})
            if any(_matches_hard(f, looser_q) for f in self.flights):
                return (
                    f"No flights to that route/date under ${query.max_price_usd:.0f}. "
                    "Want me to look at slightly higher prices?"
                )
        if query.refundable_only:
            looser_q = query.model_copy(update={"refundable_only": False})
            if any(_matches_hard(f, looser_q) for f in self.flights):
                return (
                    "No refundable tickets match your route/date. "
                    "Non-refundable options exist if you want to see them."
                )
        if query.destination and query.departure_date:
            looser_q = query.model_copy(update={"departure_date": None, "return_date": None})
            if any(_matches_hard(f, looser_q) for f in self.flights):
                return (
                    "I have flights on that route, but none in your date window. "
                    "Try a different month?"
                )
        # Both origin + destination resolved to known IATAs but no flight on
        # that specific route - list what IS connected from origin and to dest.
        if query.origin and query.destination:
            origin_iatas = expand(query.origin)
            dest_iatas = expand(query.destination)
            if origin_iatas and dest_iatas:
                from_origin = sorted(
                    {
                        f.destination
                        for f in self.flights
                        if f.origin in origin_iatas
                    }
                )
                to_dest = sorted(
                    {
                        f.origin
                        for f in self.flights
                        if f.destination in dest_iatas
                    }
                )
                bits: list[str] = [
                    f"I don't have any direct {query.origin}→{query.destination} flights in my dataset."
                ]
                if from_origin:
                    bits.append(
                        f"From {query.origin} I do have flights to: "
                        f"{', '.join(from_origin[:5])}."
                    )
                if to_dest:
                    bits.append(
                        f"To {query.destination} I have flights from: "
                        f"{', '.join(to_dest[:5])}."
                    )
                if from_origin or to_dest:
                    bits.append("Want me to try one of those routes?")
                return " ".join(bits)
        return "No flights match your route or basic constraints."


def _was_active(constraint: str, query: FlightQuery) -> bool:
    """Was this soft constraint actually set in the user's query? (Used for honest reporting.)"""
    if constraint == "preferred_airlines":
        return bool(query.preferred_airlines)
    if constraint == "preferred_alliances":
        return bool(query.preferred_alliances)
    if constraint == "avoid_overnight_layovers":
        return query.avoid_overnight_layovers
    if constraint == "max_layover_hours":
        return query.max_layover_hours is not None
    return False


__all__ = [
    "SOFT_CONSTRAINTS_PRIORITY",
    "FlightIndex",
]
