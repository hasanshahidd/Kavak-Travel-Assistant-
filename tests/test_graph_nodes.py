"""Per-node tests for router, extractor, clarifier, out_of_scope.

Each node is a pure callable that takes substrate + state, calls the LLM,
emits a trace event, and returns structured output. These tests use
``MockClient`` to register canned responses for each prompt id and
assert the right output + trace event fires.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.graph.nodes.clarifier import clarify
from app.graph.nodes.extractor import extract
from app.graph.nodes.out_of_scope import out_of_scope_reply
from app.graph.nodes.router import route
from app.llm.client import MockClient
from app.llm.tracing import Tracer
from app.schemas.flight import FlightQuery
from app.schemas.intent import Intent, RouterOutput
from app.schemas.oos import OOSReply


@pytest.fixture
def tracer(tmp_path: Path) -> Tracer:
    return Tracer(turn_id="node-test", trace_dir=tmp_path / "traces", redact=False)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_returns_classified_intent(tracer: Tracer) -> None:
    canned = RouterOutput(intent=Intent.FLIGHT_SEARCH, rationale="user mentioned Tokyo and August")
    client = MockClient()
    client.register("router.v10", raw_text=canned.model_dump_json(), parsed=canned)

    out = route(
        user_message="Round-trip Dubai to Tokyo in August",
        conversation_summary="(no prior)",
        client=client,
        tracer=tracer,
    )
    assert out.intent is Intent.FLIGHT_SEARCH
    assert tracer.events[0].node == "router"
    assert tracer.events[0].output["intent"] == "flight_search"


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


def test_extractor_returns_flight_query_with_no_prior(tracer: Tracer) -> None:
    canned = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
    )
    client = MockClient()
    client.register("extractor.v4", raw_text=canned.model_dump_json(), parsed=canned)

    merged = extract(
        user_message="round-trip Dubai to Tokyo in August",
        conversation_summary="(no prior)",
        prior_query=None,
        client=client,
        tracer=tracer,
    )
    assert merged.origin == "Dubai"
    assert merged.destination == "Tokyo"
    assert tracer.events[0].node == "extractor"
    assert tracer.events[0].output["had_prior"] is False
    assert tracer.events[0].output["topic_switch"] is False


def test_extractor_merges_with_prior_for_sparse_new_query(tracer: Tracer) -> None:
    """When the LLM returns only the changed field, the merge fills the rest."""
    prior = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        departure_date=date(2026, 8, 1),
        preferred_alliances=["Star Alliance"],
    )
    sparse_new = FlightQuery(max_price_usd=800)
    client = MockClient()
    client.register("extractor.v4", raw_text=sparse_new.model_dump_json(), parsed=sparse_new)

    merged = extract(
        user_message="make it cheaper",
        conversation_summary="prior: Dubai→Tokyo, Star Alliance",
        prior_query=prior,
        client=client,
        tracer=tracer,
    )
    # Prior fields inherited
    assert merged.destination == "Tokyo"
    assert merged.preferred_alliances == ["Star Alliance"]
    # New constraint applied
    assert merged.max_price_usd == 800
    # Trace records the merge happened
    assert tracer.events[0].output["had_prior"] is True


def test_extractor_records_topic_switch_in_trace(tracer: Tracer) -> None:
    prior = FlightQuery(
        origin="Dubai",
        destination="Tokyo",
        preferred_alliances=["Star Alliance"],
        avoid_overnight_layovers=True,
    )
    new = FlightQuery(origin="Dubai", destination="Paris")
    client = MockClient()
    client.register("extractor.v4", raw_text=new.model_dump_json(), parsed=new)

    merged = extract(
        user_message="now show me flights to Paris",
        conversation_summary="prior: Dubai→Tokyo",
        prior_query=prior,
        client=client,
        tracer=tracer,
    )
    assert merged.destination == "Paris"
    # Topic switch reset preferences
    assert merged.preferred_alliances == []
    assert merged.avoid_overnight_layovers is False
    assert tracer.events[0].output["topic_switch"] is True


# ---------------------------------------------------------------------------
# Clarifier
# ---------------------------------------------------------------------------


def test_clarifier_returns_text_question(tracer: Tracer) -> None:
    canned = "What city are you flying from?"
    client = MockClient(default_text=canned)
    text = clarify(
        user_message="flights to Bali next month",
        conversation_summary="(no prior)",
        missing_fields=["origin"],
        client=client,
        tracer=tracer,
    )
    assert text == canned
    assert tracer.events[0].node == "clarifier"
    assert tracer.events[0].output["missing_fields"] == ["origin"]


# ---------------------------------------------------------------------------
# Out-of-scope - LLM-driven (v3, replaces regex/whitelist branching)
# ---------------------------------------------------------------------------


def _register_oos(client: MockClient, category: str, reply: str) -> None:
    """Helper: pre-load a canned OOSReply on the mock client."""
    canned = OOSReply(category=category, reply=reply)
    client.register("oos_reply.v4", raw_text=canned.model_dump_json(), parsed=canned)


# Stub inventories for OOS unit tests - the prompt requires these variables
# but the MockClient ignores body content, so any non-empty string suffices.
_FLIGHT_INV = "Origins: Dubai. Destinations: Tokyo. Sample routes: Dubai→Tokyo."
_KB_INV = "visa rules: Japan. refund policy: cancellation window."


def test_oos_returns_oos_reply_object(tracer: Tracer) -> None:
    client = MockClient()
    _register_oos(client, "redirect", "Weather is outside what I cover. Want flights?")

    result = out_of_scope_reply(
        user_message="what's the weather in Tokyo",
        client=client,
        flight_inventory=_FLIGHT_INV,
        kb_inventory=_KB_INV,
        tracer=tracer,
    )
    assert isinstance(result, OOSReply)
    assert result.category == "redirect"
    assert "Weather" in result.reply


def test_oos_calls_llm_with_oos_reply_prompt(tracer: Tracer) -> None:
    client = MockClient()
    _register_oos(client, "greeting", "Hi! I help with flights. What's your route?")

    out_of_scope_reply(
        user_message="hello",
        client=client,
        flight_inventory=_FLIGHT_INV,
        kb_inventory=_KB_INV,
        tracer=tracer,
    )
    assert tracer.events[0].node == "out_of_scope"
    assert tracer.events[0].prompt_id == "oos_reply.v4"
    assert tracer.events[0].output["category"] == "greeting"


def test_oos_trace_records_category(tracer: Tracer) -> None:
    """The trace must capture the LLM-emitted category for the badge."""
    client = MockClient()
    _register_oos(client, "info", "I help with flights and travel-policy questions.")

    out_of_scope_reply(
        user_message="what can you do",
        client=client,
        flight_inventory=_FLIGHT_INV,
        kb_inventory=_KB_INV,
        tracer=tracer,
    )
    assert tracer.events[0].output["category"] == "info"


def test_oos_invalid_response_type_raises() -> None:
    """If the client returns the wrong type, fail fast - don't ship garbage."""
    client = MockClient(default_text="not a valid OOSReply JSON")
    # The MockClient will try to parse the default text as OOSReply,
    # which will raise LLMValidationError before our TypeError check.
    with pytest.raises(Exception):  # noqa: B017 - broad on purpose
        out_of_scope_reply(
            user_message="hi",
            client=client,
            flight_inventory=_FLIGHT_INV,
            kb_inventory=_KB_INV,
        )


def test_oos_handles_all_three_categories() -> None:
    """Smoke test all three sub-categories the prompt can return."""
    for cat, msg in [
        ("greeting", "Hi! What's your destination?"),
        ("info", "I help with flights and travel-policy questions."),
        ("redirect", "I focus on flights. Want me to look up a route?"),
    ]:
        client = MockClient()
        _register_oos(client, cat, msg)
        result = out_of_scope_reply(
            user_message="test",
            client=client,
            flight_inventory=_FLIGHT_INV,
            kb_inventory=_KB_INV,
        )
        assert result.category == cat
        assert result.reply == msg


def test_oos_integration_with_real_user_phrasings_via_mock() -> None:
    """The mock returns whatever we register; real LLM behaviour tested in
    full agent integration tests against opt-in OpenAI."""
    client = MockClient()
    _register_oos(
        client,
        "greeting",
        "Wa alaikum salaam! I help with flight search. What can I do for you?",
    )
    result = out_of_scope_reply(
        user_message="assalamu alaikum",
        client=client,
        flight_inventory=_FLIGHT_INV,
        kb_inventory=_KB_INV,
    )
    assert result.category == "greeting"
    assert "salaam" in result.reply.lower()
