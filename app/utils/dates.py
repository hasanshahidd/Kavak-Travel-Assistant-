"""Date helpers for relative-date resolution and month-precision matching.

Two responsibilities:

1. **Pin "today"** to a deterministic value so prompts and tests give the
   same answer every time. The extractor prompt names this date inline so
   the LLM resolves "next August" against it; this module is the single
   source of truth callers should reach for if they need it programmatically.

2. **Month-precision matching.** When the user says "in August", the
   extractor sets ``departure_date = 2026-08-01``. The flight tool treats
   this as a *month* preference, not a specific day, because users almost
   never mean a literal first-of-month flight. ``matches_month`` codifies
   that semantic.
"""

from __future__ import annotations

from datetime import date

# Pinned for reproducibility. If the calendar date in the project moves,
# update this and the extractor prompt's "Today is ..." line in lockstep.
TODAY: date = date(2026, 5, 2)


def matches_month(flight_date: date, target: date | None) -> bool:
    """True iff ``flight_date`` falls in the same calendar month as ``target``.

    Returns True when ``target`` is None (treat as "no date constraint").
    Used by the flight index to honour user phrasing like "in August".
    """
    if target is None:
        return True
    return (flight_date.year, flight_date.month) == (target.year, target.month)


def month_label(d: date) -> str:
    """Human-friendly month label, e.g. 'August 2026'."""
    return d.strftime("%B %Y")


__all__ = ["TODAY", "matches_month", "month_label"]
