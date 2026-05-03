"""Conversation memory + override semantics - the D4 differentiator.

Most chat agents fail multi-turn refinement in one of two ways:

* **State leak.** "Now show me flights to Paris" still applies last turn's
  Star Alliance + no-overnight filters, returning empty results from a route
  that has plenty of options.
* **State amnesia.** "Make it cheaper" produces a fresh search with no
  destination because the model treats every turn as turn one.

This module is the deterministic safety net that makes neither of those
happen, even when the extractor's prompt-level override logic falters.

The contract:

* :func:`merge_query` takes a prior :class:`FlightQuery` and a new one and
  returns the merged query. It detects topic switches (destination changed
  to a different city) and returns the new query unchanged in that case.
  For non-switches, ``None`` fields in the new query inherit the prior
  value - covering the "make it cheaper" pattern where the extractor only
  fills the field the user actually changed.

* :class:`Conversation` is the per-session memory store. It holds the
  message log, the latest :class:`FlightQuery`, and produces a textual
  ``summary()`` that the extractor prompt sees as ``conversation_summary``.

The override semantics are tested in ``tests/test_memory_override.py`` -
five scenarios covering the spec's worst failure modes.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

from app.schemas.chat import ChatMessage
from app.schemas.flight import FlightQuery
from app.utils.airports import expand

# Sliding window size - older messages get summarised. Six is a sweet spot:
# enough to recall a 3-turn refinement loop, short enough to keep prompt
# tokens bounded.
DEFAULT_WINDOW = 6


# ---------------------------------------------------------------------------
# Topic-switch detection
# ---------------------------------------------------------------------------


def _is_same_destination(prior: str | None, new: str | None) -> bool:
    """Loose equality - uses the airport alias map so 'Tokyo' == 'NRT' == 'HND'.

    Both None → trivially same. One None and one set → different. Else: do
    their IATA expansions intersect?
    """
    if prior is None and new is None:
        return True
    if prior is None or new is None:
        return False
    if prior.strip().lower() == new.strip().lower():
        return True
    p = expand(prior)
    n = expand(new)
    if not p or not n:
        # Unknown city on either side → fall back to literal compare
        return prior.strip().lower() == new.strip().lower()
    return bool(p & n)


def is_topic_switch(prior: FlightQuery | None, new: FlightQuery) -> bool:
    """True iff the new query targets a different destination than the prior.

    A bare ``None`` destination on the new query is *not* a switch - the user
    might be refining without re-naming the destination ("make it cheaper").
    Only an explicit, *different* destination triggers a reset.
    """
    if prior is None or prior.destination is None:
        return False
    if new.destination is None:
        return False
    return not _is_same_destination(prior.destination, new.destination)


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------


# Fields whose ``None`` in the new query means "inherit from prior".
# Anything *not* in this list (e.g. ``needs_clarification``, ``scratchpad``)
# always takes the new value - those are per-turn signals, not user
# preferences worth preserving.
_INHERITABLE_FIELDS: tuple[str, ...] = (
    "origin",
    "destination",
    "departure_date",
    "return_date",
    "trip_type",
    "preferred_airlines",
    "preferred_alliances",
    "excluded_airlines",
    "excluded_alliances",
    "max_layover_hours",
    "avoid_overnight_layovers",
    "max_price_usd",
    "refundable_only",
)

# Fields where "empty list / False / no preference" should inherit prior
# preferences if the user didn't restate them. Distinguishes "unset" from
# "explicitly cleared" - the extractor produces False for booleans by
# default, so a False here doesn't mean "user said no".
_LIST_FIELDS: tuple[str, ...] = (
    "preferred_airlines",
    "preferred_alliances",
    "excluded_airlines",
    "excluded_alliances",
)
_DEFAULT_FALSE_FIELDS: tuple[str, ...] = ("avoid_overnight_layovers", "refundable_only")


def merge_query(prior: FlightQuery | None, new: FlightQuery) -> FlightQuery:
    """Merge the new extractor output with prior conversation state.

    Three behaviours, in order of evaluation:

    1. **No prior state** → return ``new`` as-is.
    2. **Topic switch** (destination changed) → return ``new`` as-is, dropping
       all prior preferences. The user moved on; nothing carries over.
    3. **Refinement** → fill ``None`` / empty fields on ``new`` from ``prior``.
       Explicit values on ``new`` always win; only unspecified fields
       inherit. This covers "make it cheaper", "actually move it to
       September", "what about September instead" - phrasings where the
       extractor only emits the field the user actually changed.
    """
    if prior is None:
        return new
    if is_topic_switch(prior, new):
        return new

    updates: dict[str, object] = {}
    for field in _INHERITABLE_FIELDS:
        new_val = getattr(new, field)
        prior_val = getattr(prior, field)

        # Explicit list provided on the new query → use it.
        # Empty list on new + non-empty on prior → inherit (user didn't restate prefs).
        if field in _LIST_FIELDS:
            if not new_val and prior_val:
                updates[field] = prior_val
            continue

        # Boolean preferences default to False. We can't distinguish
        # "user didn't mention overnights" from "user said overnights are fine"
        # at the schema level - convention: prior True wins unless new is
        # explicitly False AND prior was False (no change). The extractor's
        # job is to emit True only when the user said so; otherwise leave it
        # at the default. So a False on new + True on prior → inherit True.
        if field in _DEFAULT_FALSE_FIELDS:
            if new_val is False and prior_val is True:
                updates[field] = prior_val
            continue

        # Generic "None on new" → inherit.
        if new_val is None and prior_val is not None:
            updates[field] = prior_val

    if not updates:
        return new
    return new.model_copy(update=updates)


# ---------------------------------------------------------------------------
# Conversation store
# ---------------------------------------------------------------------------


class Conversation:
    """Per-session memory. Holds messages, prior query, and a textual summary.

    Not async-safe out of the box - add a lock if you ever share a single
    Conversation across coroutines. Single-user CLI / Streamlit usage
    doesn't need it.
    """

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self._window = window
        self._messages: deque[ChatMessage] = deque(maxlen=window * 2)
        self._prior_query: FlightQuery | None = None
        self._summary_lines: list[str] = []

    # ------- mutation -------

    def add_user_message(self, content: str) -> ChatMessage:
        msg = ChatMessage(role="user", content=content, timestamp=datetime.now(UTC))
        self._messages.append(msg)
        return msg

    def add_assistant_message(self, content: str) -> ChatMessage:
        msg = ChatMessage(role="assistant", content=content, timestamp=datetime.now(UTC))
        self._messages.append(msg)
        return msg

    def commit_query(self, query: FlightQuery | None) -> None:
        """Store the merged query as the new prior. Called at end of turn."""
        if query is None:
            return
        self._prior_query = query
        # Add a one-line "remember" entry to the summary so the extractor
        # sees relevant prior state in its `conversation_summary` slot.
        self._summary_lines.append(self._summarise_query(query))
        # Cap summary to last few entries to keep token use bounded.
        if len(self._summary_lines) > self._window:
            self._summary_lines = self._summary_lines[-self._window :]

    def reset(self) -> None:
        """Clear everything. Used by ``/reset`` in the CLI."""
        self._messages.clear()
        self._prior_query = None
        self._summary_lines.clear()

    # ------- reads -------

    @property
    def prior_query(self) -> FlightQuery | None:
        return self._prior_query

    @property
    def messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def windowed_messages(self) -> list[ChatMessage]:
        """Most-recent ``window`` messages - what we feed back to prompts."""
        return list(self._messages)[-self._window :]

    def summary(self) -> str:
        """Textual summary of state worth telling the extractor about.

        Prompts receive this as their ``conversation_summary`` variable.
        Returns a non-empty placeholder when nothing's happened yet so the
        prompt template substitution always produces a sensible string.
        """
        if not self._summary_lines and not self._messages:
            return "(no prior conversation)"
        prior = "\n".join(f"- {line}" for line in self._summary_lines)
        recent = "\n".join(
            f"  {m.role}: {m.content[:140]}" for m in self.windowed_messages()
        )
        parts: list[str] = []
        if prior:
            parts.append(f"Prior search state:\n{prior}")
        if recent:
            parts.append(f"Recent turns:\n{recent}")
        return "\n\n".join(parts) if parts else "(no prior conversation)"

    # ------- helpers -------

    @staticmethod
    def _summarise_query(q: FlightQuery) -> str:
        bits: list[str] = []
        if q.origin and q.destination:
            bits.append(f"{q.origin}→{q.destination}")
        elif q.destination:
            bits.append(f"to {q.destination}")
        if q.departure_date:
            bits.append(q.departure_date.strftime("%b %Y"))
        if q.preferred_alliances:
            bits.append("/".join(q.preferred_alliances))
        if q.avoid_overnight_layovers:
            bits.append("no overnight")
        if q.max_price_usd is not None:
            bits.append(f"under ${q.max_price_usd:.0f}")
        if q.refundable_only:
            bits.append("refundable")
        return ", ".join(bits) if bits else "(open search)"


__all__ = [
    "DEFAULT_WINDOW",
    "Conversation",
    "is_topic_switch",
    "merge_query",
]
