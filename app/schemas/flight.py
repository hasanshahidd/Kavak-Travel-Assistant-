"""Flight domain schemas.

Three concentric layers:
- ``FlightQuery``: structured filter extracted from natural language by the LLM
- ``Flight``: a row from the mock dataset
- ``FlightResult``: a ranked match with explanation, returned to the user
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TripType(StrEnum):
    """One-way vs round-trip."""

    ONE_WAY = "one_way"
    ROUND_TRIP = "round_trip"


# ---------------------------------------------------------------------------
# FlightQuery - what the extractor produces
# ---------------------------------------------------------------------------


class FlightQuery(BaseModel):
    """Structured user intent for a flight search.

    Every field is optional so the extractor can express partial knowledge.
    The clarifier node uses ``needs_clarification`` + ``missing_fields`` to
    decide whether to ask a follow-up question instead of guessing.

    The ``scratchpad`` field implements hidden chain-of-thought: the model
    reasons aloud here (e.g. resolving "next August") before committing to
    structured fields. It's stored in the trace but never shown to the user.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Core ---
    origin: str | None = Field(
        None, description="IATA code or city name (e.g. 'DXB' or 'Dubai')."
    )
    destination: str | None = Field(
        None, description="IATA code or city name."
    )
    departure_date: date | None = None
    return_date: date | None = None
    trip_type: TripType = TripType.ROUND_TRIP

    # --- Preferences ---
    preferred_alliances: list[str] = Field(
        default_factory=list,
        description="e.g. ['Star Alliance']. Empty = no preference.",
    )
    preferred_airlines: list[str] = Field(
        default_factory=list,
        description="e.g. ['Emirates']. Combined with preferred_alliances via OR.",
    )
    excluded_alliances: list[str] = Field(
        default_factory=list,
        description=(
            "Alliances the user explicitly does NOT want. e.g. user says "
            "'not Star Alliance' or 'no SkyTeam' → ['Star Alliance']. "
            "Hard exclusion - flights matching any of these alliances are "
            "filtered out before ranking."
        ),
    )
    excluded_airlines: list[str] = Field(
        default_factory=list,
        description=(
            "Airlines the user explicitly does NOT want. e.g. 'no Emirates' "
            "→ ['Emirates']. Hard exclusion."
        ),
    )

    # --- Layover ---
    max_layover_hours: float | None = Field(
        None, ge=0, le=48, description="Per-leg cap on layover duration."
    )
    avoid_overnight_layovers: bool = False

    # --- Cost & flexibility ---
    max_price_usd: Annotated[float | None, Field(None, ge=0)] = None
    refundable_only: bool = False

    # --- Reasoning + clarification ---
    scratchpad: str = Field(
        default="",
        description=(
            "Hidden chain-of-thought. Model reasons here before filling structured "
            "fields. Captured in trace logs; never shown to the user."
        ),
        max_length=2000,
    )
    needs_clarification: bool = False
    missing_fields: list[str] = Field(
        default_factory=list,
        description="Fields the model could not determine and wants the user to provide.",
    )

    # --- Result-shape hints from the user ---
    result_count_hint: int | None = Field(
        None,
        ge=1,
        le=10,
        description=(
            "User asked for a specific number of results, e.g. 'show me the cheapest one' "
            "→ 1, 'top 5' → 5. None means use the system default (3). The flight tool "
            "honours this by clamping its top-K return."
        ),
    )
    sort_by: Annotated[str, Field(pattern="^(best|price)$")] = Field(
        default="best",
        description=(
            "Ranking axis. 'best' (default) uses the composite score that "
            "balances price + layover quality + refundability. 'price' sorts "
            "by raw price ascending - set this when the user explicitly asks "
            "for the cheapest / lowest price / under-N-dollars."
        ),
    )

    # --- Validators ---
    @field_validator("preferred_alliances", "preferred_airlines", "excluded_alliances", "excluded_airlines", "missing_fields")
    @classmethod
    def _strip_strings(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s and s.strip()]

    @model_validator(mode="after")
    def _check_dates(self) -> FlightQuery:
        if (
            self.departure_date
            and self.return_date
            and self.return_date < self.departure_date
        ):
            raise ValueError("return_date cannot be before departure_date")
        if self.trip_type is TripType.ONE_WAY and self.return_date is not None:
            # Tolerate the model setting both; one-way wins, drop return.
            self.return_date = None
        return self


# ---------------------------------------------------------------------------
# Flight - a row from data/flights.json
# ---------------------------------------------------------------------------


class Flight(BaseModel):
    """A single flight option in the mock dataset."""

    model_config = ConfigDict(extra="forbid")

    id: str
    airline: str
    alliance: str | None = Field(
        None,
        description="One of 'Star Alliance', 'OneWorld', 'SkyTeam', or None.",
    )
    origin: str = Field(..., min_length=3, max_length=3, description="IATA code")
    destination: str = Field(..., min_length=3, max_length=3)
    departure_date: date
    return_date: date | None = None
    layovers: list[str] = Field(default_factory=list, description="IATA codes")
    layover_hours: float = Field(0.0, ge=0)
    is_overnight_layover: bool = False
    price_usd: float = Field(..., gt=0)
    refundable: bool

    @field_validator("origin", "destination")
    @classmethod
    def _upper_iata(cls, v: str) -> str:
        return v.upper()

    @field_validator("layovers")
    @classmethod
    def _upper_layovers(cls, v: list[str]) -> list[str]:
        return [s.upper() for s in v]

    @model_validator(mode="after")
    def _layover_consistency(self) -> Flight:
        if self.layovers and self.layover_hours == 0:
            # Direct flights have no layover_hours; presence of layovers requires duration.
            raise ValueError(
                f"Flight {self.id}: has layovers {self.layovers} but layover_hours=0"
            )
        if not self.layovers and self.is_overnight_layover:
            raise ValueError(
                f"Flight {self.id}: cannot be overnight without layovers"
            )
        return self


# ---------------------------------------------------------------------------
# FlightResult - what the responder formats for the user
# ---------------------------------------------------------------------------


class FlightResult(BaseModel):
    """A ranked flight match with explanation."""

    model_config = ConfigDict(extra="forbid")

    flight: Flight
    score: float = Field(..., description="Composite ranking score; lower is better.")
    explanation: str = Field(
        ...,
        description="One-line rationale, e.g. 'Cheapest non-stop, refundable, Star Alliance'.",
        max_length=200,
    )
    relaxed_constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Constraints the search relaxed to surface this result. Empty if it "
            "matches the user query exactly. Surfaced to the user for transparency."
        ),
    )


# ---------------------------------------------------------------------------
# SearchOutcome - what the flight tool returns to the responder
# ---------------------------------------------------------------------------


class SearchOutcome(BaseModel):
    """Result envelope from the flight index.

    Carries everything the responder needs to write an honest reply:
    top-N matches, *which* (if any) constraints we relaxed to surface them,
    the total candidate pool size pre-ranking, and a human-readable reason
    when even the most generous relaxation found nothing.
    """

    model_config = ConfigDict(extra="forbid")

    results: list[FlightResult] = Field(
        default_factory=list,
        description="Top-N ranked matches (lowest composite score first).",
    )
    relaxed_constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Soft constraints that were dropped before any results were found. "
            "Empty list means the user's query was satisfied exactly."
        ),
    )
    total_matched: int = Field(
        0,
        ge=0,
        description="Pre-ranking count of flights that satisfied all (relaxed) constraints.",
    )
    no_results_reason: str | None = Field(
        None,
        description=(
            "Human-readable explanation when results is empty even after full "
            "relaxation, e.g. 'no flights to that destination in your date window'."
        ),
    )

    @property
    def is_relaxed(self) -> bool:
        return bool(self.relaxed_constraints)


# ---------------------------------------------------------------------------
# ResponseCritique - output of the self-critique step on the flight responder
# ---------------------------------------------------------------------------


class ResponseCritique(BaseModel):
    """Self-critique result for a draft flight reply.

    The responder generates a draft, then a critique LLM call evaluates it
    against three checkpoints: factual accuracy (no fabricated airlines /
    prices / dates), structural completeness (relaxation note when needed,
    one-line rationale per flight), and conversational hygiene (single
    follow-up question, no chatty preamble).

    When ``needs_revision=True`` the responder runs a second pass with
    ``issues`` injected as feedback. The eval suite A/B-tests this loop:
    we expect a measurable quality bump on borderline cases at the cost
    of one extra LLM call per turn.
    """

    model_config = ConfigDict(extra="forbid")

    needs_revision: bool = Field(
        ...,
        description="True iff the draft has at least one critical issue worth revising.",
    )
    issues: list[str] = Field(
        default_factory=list,
        description=(
            "Specific, actionable problems found in the draft. Empty when "
            "needs_revision=False. Used as revision feedback when True."
        ),
    )
    confidence: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Self-reported confidence in this critique. Useful for trace inspection.",
    )
