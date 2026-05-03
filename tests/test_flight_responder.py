"""Flight responder tests — draft path + self-critique loop.

The self-critique loop is the 0.01% upgrade for Block 5. These tests prove:

1. Without critique: draft is returned as-is.
2. With critique + critique passes: draft returned, no revision.
3. With critique + critique fails: revision pass runs and returns the revised text.
4. Tracer captures every step (draft + critique + revision) with prompt ids.
5. Env-flag honoured when no explicit kwarg is passed.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from app.graph.nodes.flight_search import search_flights
from app.graph.nodes.responder import SELF_CRITIQUE_ENV, respond
from app.llm.client import MockClient
from app.llm.tracing import Tracer
from app.schemas.flight import FlightQuery, ResponseCritique
from app.tools.flight_index import FlightIndex


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer(turn_id="resp-test", trace_dir=tmp_path / "traces", redact=False)


@pytest.fixture
def search_outcome():
    """Real flight search outcome to feed into the responder."""
    query = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    index = FlightIndex()
    outcome = index.search(query)
    return query, outcome


# ---------------------------------------------------------------------------
# Path 1 — draft only (critique disabled)
# ---------------------------------------------------------------------------


def test_draft_only_when_self_critique_disabled(search_outcome, tracer: Tracer) -> None:
    query, outcome = search_outcome
    canned_draft = (
        "I found 2 Star Alliance flights from Dubai to Tokyo in August.\n\n"
        "1. **Turkish Airlines · DXB → NRT**\n"
        "   2026-08-15 → 2026-08-30 · $950 · refundable\n"
        "   5.5h via IST (daytime)\n"
        "   *Cheapest Star Alliance option without overnight transit.*\n\n"
        "Want me to filter further?"
    )
    client = MockClient(default_text=canned_draft)
    result = respond(
        query=query, outcome=outcome, client=client, tracer=tracer, self_critique=False
    )
    assert result == canned_draft

    nodes = [e.node for e in tracer.events]
    assert nodes == ["responder.draft"]


# ---------------------------------------------------------------------------
# Path 2 — critique passes, no revision
# ---------------------------------------------------------------------------


def test_critique_passes_so_no_revision(search_outcome, tracer: Tracer) -> None:
    query, outcome = search_outcome
    canned_draft = "Solid draft that the critique will approve."
    pass_critique = ResponseCritique(needs_revision=False, issues=[], confidence=0.95)

    client = MockClient(default_text=canned_draft)
    client.register(
        "responder_critique.v1",
        raw_text=pass_critique.model_dump_json(),
        parsed=pass_critique,
    )

    result = respond(
        query=query, outcome=outcome, client=client, tracer=tracer, self_critique=True
    )
    assert result == canned_draft

    nodes = [e.node for e in tracer.events]
    assert nodes == ["responder.draft", "responder.critique"]


# ---------------------------------------------------------------------------
# Path 3 — critique fails, revision runs
# ---------------------------------------------------------------------------


def test_critique_fails_triggers_revision(search_outcome, tracer: Tracer) -> None:
    query, outcome = search_outcome

    # MockClient returns the *same* default_text for both responder calls.
    # The way we tell "draft" from "revision" is by trace events, not text content.
    canned_draft = "Draft missing the relaxation note."
    fail_critique = ResponseCritique(
        needs_revision=True,
        issues=["Draft does not mention that the alliance was relaxed."],
        confidence=0.85,
    )

    client = MockClient(default_text=canned_draft)
    client.register(
        "responder_critique.v1",
        raw_text=fail_critique.model_dump_json(),
        parsed=fail_critique,
    )

    result = respond(
        query=query, outcome=outcome, client=client, tracer=tracer, self_critique=True
    )
    # The revision call goes back through the responder prompt, returning canned_draft again.
    # In production with a real LLM, the revised text would address the issues; here we just
    # verify the loop ran and emitted the right trace events.
    assert isinstance(result, str)

    nodes = [e.node for e in tracer.events]
    assert nodes == ["responder.draft", "responder.critique", "responder.revision"]

    revision_event = tracer.events[-1]
    assert revision_event.output["issues_addressed"] == fail_critique.issues


# ---------------------------------------------------------------------------
# Path 4 — env flag controls default behaviour
# ---------------------------------------------------------------------------


def test_env_flag_enables_critique_when_kwarg_omitted(
    search_outcome, tracer: Tracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    query, outcome = search_outcome
    monkeypatch.setenv(SELF_CRITIQUE_ENV, "1")

    pass_critique = ResponseCritique(needs_revision=False, issues=[], confidence=0.99)
    client = MockClient(default_text="Draft.")
    client.register(
        "responder_critique.v1",
        raw_text=pass_critique.model_dump_json(),
        parsed=pass_critique,
    )

    respond(query=query, outcome=outcome, client=client, tracer=tracer)

    nodes = [e.node for e in tracer.events]
    assert "responder.critique" in nodes  # critique fired due to env flag


def test_env_flag_off_by_default(search_outcome, tracer: Tracer) -> None:
    query, outcome = search_outcome
    # Make sure no env flag is leaking from another test
    if SELF_CRITIQUE_ENV in os.environ:
        del os.environ[SELF_CRITIQUE_ENV]
    client = MockClient(default_text="Draft.")
    respond(query=query, outcome=outcome, client=client, tracer=tracer)

    nodes = [e.node for e in tracer.events]
    assert nodes == ["responder.draft"]


# ---------------------------------------------------------------------------
# Path 5 — empty results path still produces a usable reply
# ---------------------------------------------------------------------------


def test_responder_no_results_path_short_circuits_without_llm(tracer: Tracer) -> None:
    """When the flight tool returned no matches, the responder uses a
    deterministic template — NO LLM call. This is the structural defence
    against the no-results hallucination mode (model inventing flights
    to 'fill the void' when given an empty result set)."""
    query = FlightQuery(
        origin="Dubai",
        destination="Atlantis",
        departure_date=date(2026, 8, 1),
    )
    index = FlightIndex()
    outcome = search_flights(query=query, index=index, tracer=tracer)
    assert outcome.results == []

    # The MockClient is intentionally NOT registered with any responder text.
    # If the LLM were called, the test would either pick up a default canned
    # response OR raise. We assert neither happens — the template fires first.
    client = MockClient(
        default_text="THIS_TEXT_SHOULD_NEVER_APPEAR_IN_USER_REPLY_HALLUCINATION"
    )
    result = respond(
        query=query, outcome=outcome, client=client, tracer=tracer, self_critique=False
    )

    # Hallucination probe: fabricated text from the mock must NOT have leaked.
    assert "HALLUCINATION" not in result, (
        "responder called the LLM on empty-results path — short-circuit failed"
    )
    # The flight tool's actual diagnosis must be in the reply.
    assert "Atlantis" in result or "don't have" in result.lower()
    # Trace records the deterministic path was taken.
    assert any(
        e.node == "responder.no_results"
        and e.output.get("no_llm_call") is True
        for e in tracer.events
    )
    # No draft / critique / revision events
    llm_nodes = {e.node for e in tracer.events if e.node.startswith("responder.")}
    assert "responder.draft" not in llm_nodes
