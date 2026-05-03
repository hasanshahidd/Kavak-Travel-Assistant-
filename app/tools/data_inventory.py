"""Runtime inventory of what the agent actually has - flight catalogue + KB topics.

The OOS reply LLM uses this to answer scope/meta queries ("what visas do
you cover", "do you fly to Bali", "what can you do") from *real data*
rather than from hardcoded coverage strings baked into the prompt.

Two consequences worth flagging:

1. **No prompt bumps when data changes.** Add a new visa doc to the KB
   or a new flight row to the catalogue and the bot's scope replies
   update on the next turn - no edit to ``oos_reply.md`` required.

2. **No fabrication risk.** The inventory text is composed from the same
   structured sources the rest of the agent consumes (``FlightIndex.flights``,
   ``KBRetriever`` chunk metadata). The LLM is constrained by the prompt
   to answer only from this block - it's the same anti-hallucination
   contract used in the RAG path, applied to scope questions.

The strings are deliberately short (route lists capped at 8, KB sections
one bullet per H2) so they fit in the OOS prompt without budget pressure.
"""

from __future__ import annotations

from app.tools.flight_index import FlightIndex
from app.tools.kb_retriever import KBRetriever
from app.utils.airports import city_for

_NO_FLIGHTS = "(no flights loaded)"
_NO_KB = "(no policy documents loaded)"


def _label(iata: str) -> str:
    """City name when we know it; otherwise the IATA code itself."""
    return city_for(iata) or iata


def flight_inventory(index: FlightIndex, *, max_routes: int = 8) -> str:
    """One short paragraph describing the flight dataset.

    Lists distinct origin cities, destination cities, alliances, and a
    sample of routes. Capped to keep the prompt budget tight - for a
    larger catalogue we'd switch to "X origins / Y destinations" summaries.
    """
    flights = index.flights
    if not flights:
        return _NO_FLIGHTS

    origins = sorted({_label(f.origin) for f in flights})
    dests = sorted({_label(f.destination) for f in flights})
    alliances = sorted({f.alliance for f in flights if f.alliance})
    routes = sorted({f"{_label(f.origin)}→{_label(f.destination)}" for f in flights})

    bits: list[str] = []
    bits.append(f"Origins: {', '.join(origins)}.")
    bits.append(f"Destinations: {', '.join(dests)}.")
    if alliances:
        bits.append(f"Alliances: {', '.join(alliances)}.")
    sample = routes[:max_routes]
    suffix = "" if len(routes) <= max_routes else f" (+{len(routes) - max_routes} more)"
    bits.append(f"Sample routes: {'; '.join(sample)}{suffix}.")
    return " ".join(bits)


def kb_inventory(kb: KBRetriever) -> str:
    """One short paragraph describing the KB - one bullet per document.

    Each line is "doc-name: section1, section2, ...". The OOS LLM uses
    this to answer "what visas/refunds/baggage do you cover" without us
    having to keep coverage strings in the prompt body in sync with the
    KB on disk.
    """
    kb.build_or_load()
    chunks = list(kb._chunks)  # read-only access to loaded chunks
    if not chunks:
        return _NO_KB

    by_doc: dict[str, list[str]] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.doc, []).append(chunk.section)

    lines: list[str] = []
    for doc, sections in sorted(by_doc.items()):
        # Strip the .md suffix for readability; keep section names as authored.
        nice = doc.removesuffix(".md").replace("_", " ")
        lines.append(f"{nice}: {', '.join(sections)}.")
    return " ".join(lines)


__all__ = ["flight_inventory", "kb_inventory"]
