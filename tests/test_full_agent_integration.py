"""End-to-end LangGraph agent integration - proves Block 6 wires everything.

Walks a five-turn scripted conversation through the compiled agent.
Each turn exercises a different intent path, and the conversation memory
preserves state across turns. If any layer breaks - router, extractor,
flight_search, retriever, answerer, responder, clarifier, out_of_scope,
memory merge - these tests fail.

The substrate uses ``MockClient`` and ``MockEmbeddingsClient`` so this
runs without network. The opt-in test at the end (skipped without
``OPENAI_API_KEY``) walks the same flow against real OpenAI.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from app.graph.builder import AgentSubstrate, build_agent
from app.llm.client import MockClient
from app.llm.embeddings import MockEmbeddingsClient
from app.llm.tracing import Tracer
from app.memory.conversation import Conversation
from app.schemas.flight import FlightQuery
from app.schemas.intent import Intent, RouterOutput
from app.schemas.rag import Citation, RagAnswer
from app.tools.flight_index import FlightIndex
from app.tools.kb_retriever import KBRetriever

# ---------------------------------------------------------------------------
# Substrate fixture - MockClient preloaded with canned responses for each
# prompt id we'll see in the scripted conversation. This is the realism
# trade-off: we don't pay for OpenAI calls, but we do prove the wiring.
# ---------------------------------------------------------------------------


@pytest.fixture
def substrate(tmp_path: Path) -> AgentSubstrate:
    client = MockClient()
    # Real KB + flights, mock embeddings + LLM
    return AgentSubstrate(
        client=client,
        kb=KBRetriever(embeddings=MockEmbeddingsClient(), cache_dir=tmp_path / ".faiss"),
        index=FlightIndex(),
        self_critique=False,
    )


def _register_router(client: MockClient, intent: Intent, rationale: str = "test") -> None:
    canned = RouterOutput(intent=intent, rationale=rationale)
    client.register("router.v10", raw_text=canned.model_dump_json(), parsed=canned)


def _register_extractor(client: MockClient, query: FlightQuery) -> None:
    client.register("extractor.v4", raw_text=query.model_dump_json(), parsed=query)


def _register_responder(client: MockClient, text: str) -> None:
    # Responder uses default_text path - register the canned text.
    client._registry["flight_responder.v2"] = (text, None)


def _register_rag(client: MockClient, answer: RagAnswer) -> None:
    client.register("rag_answer.v3", raw_text=answer.model_dump_json(), parsed=answer)


def _register_clarifier(client: MockClient, text: str) -> None:
    client._registry["clarifier.v2"] = (text, None)


def _make_state(user_message: str, conversation: Conversation, tracer: Tracer) -> dict:
    return {
        "user_message": user_message,
        "summary": conversation.summary(),
        "prior_query": conversation.prior_query,
        "tracer": tracer,
        "turn_id": tracer.turn_id,
    }


# ---------------------------------------------------------------------------
# 5-turn scripted conversation
# ---------------------------------------------------------------------------


def test_five_turn_conversation_preserves_state_correctly(
    substrate: AgentSubstrate, tmp_path: Path
) -> None:
    """The big proof: 5 turns, every intent, memory survives, no leak."""
    convo = Conversation()
    agent = build_agent(substrate)
    client = substrate.client
    assert isinstance(client, MockClient)

    # ============ TURN 1: flight search (typical query) ============
    _register_router(client, Intent.FLIGHT_SEARCH)
    _register_extractor(
        client,
        FlightQuery(
            origin="Dubai",
            destination="Tokyo",
            departure_date=date(2026, 8, 1),
            preferred_alliances=["Star Alliance"],
            avoid_overnight_layovers=True,
        ),
    )
    _register_responder(client, "Found 2 Star Alliance flights to Tokyo without overnight layovers.")

    tracer1 = Tracer(turn_id="t1", trace_dir=tmp_path / "traces1", redact=False)
    convo.add_user_message("Round-trip Dubai to Tokyo in August, Star Alliance, no overnight")
    state1 = agent.invoke(_make_state(convo.messages[-1].content, convo, tracer1))

    assert state1["intent"] is Intent.FLIGHT_SEARCH
    assert state1["flight_query"].destination == "Tokyo"
    assert state1["flight_results"].results, "should find Star Alliance Tokyo flights"
    assert "Star Alliance" in state1["final_answer"]
    convo.commit_query(state1["flight_query"])
    convo.add_assistant_message(state1["final_answer"])

    # ============ TURN 2: refinement - date override ============
    _register_router(client, Intent.FLIGHT_SEARCH, "user is refining the prior search")
    # Sparse extractor output: only the date changed
    _register_extractor(client, FlightQuery(departure_date=date(2026, 9, 1)))
    _register_responder(client, "Updated to September. Found 1 Star Alliance match.")

    tracer2 = Tracer(turn_id="t2", trace_dir=tmp_path / "traces2", redact=False)
    convo.add_user_message("actually move it to September")
    state2 = agent.invoke(_make_state(convo.messages[-1].content, convo, tracer2))

    # The big test: merge filled in destination/alliance/no-overnight from prior
    merged_q: FlightQuery = state2["flight_query"]
    assert merged_q.departure_date == date(2026, 9, 1)
    assert merged_q.destination == "Tokyo", "destination must inherit from prior"
    assert merged_q.preferred_alliances == ["Star Alliance"], (
        "alliance preference must inherit from prior"
    )
    assert merged_q.avoid_overnight_layovers is True, "no-overnight must inherit from prior"
    convo.commit_query(merged_q)
    convo.add_assistant_message(state2["final_answer"])

    # ============ TURN 3: topic switch - reset state ============
    _register_router(client, Intent.FLIGHT_SEARCH, "destination changed")
    _register_extractor(client, FlightQuery(origin="Dubai", destination="Paris"))
    _register_responder(client, "Found Paris flights.")

    tracer3 = Tracer(turn_id="t3", trace_dir=tmp_path / "traces3", redact=False)
    convo.add_user_message("now show me flights to Paris")
    state3 = agent.invoke(_make_state(convo.messages[-1].content, convo, tracer3))

    new_q: FlightQuery = state3["flight_query"]
    assert new_q.destination == "Paris"
    # Topic switch RESET soft preferences - no leak from prior Tokyo search
    assert new_q.preferred_alliances == [], "topic switch must reset alliance preference"
    assert new_q.avoid_overnight_layovers is False, "topic switch must reset overnight constraint"
    assert new_q.departure_date is None, "topic switch must reset date"
    convo.commit_query(new_q)
    convo.add_assistant_message(state3["final_answer"])

    # ============ TURN 4: policy Q&A - different path entirely ============
    _register_router(client, Intent.POLICY_QA, "user asked about visas")
    rag = RagAnswer(
        answer="UAE passport holders can enter Japan visa-free for tourism for up to 30 days.",
        citations=[
            Citation(doc="visa_rules.md", span="visa-free for tourism for up to 30 days"),
        ],
        confidence=0.9,
    )
    _register_rag(client, rag)

    tracer4 = Tracer(turn_id="t4", trace_dir=tmp_path / "traces4", redact=False)
    convo.add_user_message("Do UAE passport holders need a visa for Japan?")
    state4 = agent.invoke(_make_state(convo.messages[-1].content, convo, tracer4))

    assert state4["intent"] is Intent.POLICY_QA
    # The RAG path either: (a) returns a verified answer with citations, or
    # (b) refuses because mock embeddings didn't surface a chunk that
    # contains the cited span verbatim. Either is a *valid* agent path -
    # what matters here is that POLICY_QA executed without polluting flight memory.
    rag = state4["rag_answer"]
    assert rag is not None
    assert rag.is_refusal or rag.citations, "RAG path must answer OR refuse, both are correct"
    convo.add_assistant_message(state4["final_answer"])
    assert convo.prior_query.destination == "Paris", (
        "policy Q&A must not overwrite the prior flight query"
    )

    # ============ TURN 5: out-of-scope - graceful redirect ============
    _register_router(client, Intent.OUT_OF_SCOPE, "weather is off-domain")
    # OOS node is now LLM-driven - register a canned OOSReply.
    from app.schemas.oos import OOSReply

    canned_oos = OOSReply(
        category="redirect",
        reply="Weather is outside what I cover - I focus on flights and travel-policy questions. Want me to look up flights to Tokyo instead?",
    )
    client.register("oos_reply.v4", raw_text=canned_oos.model_dump_json(), parsed=canned_oos)

    tracer5 = Tracer(turn_id="t5", trace_dir=tmp_path / "traces5", redact=False)
    convo.add_user_message("what's the weather in Tokyo right now?")
    state5 = agent.invoke(_make_state(convo.messages[-1].content, convo, tracer5))

    assert state5["intent"] is Intent.OUT_OF_SCOPE
    assert "outside what I cover" in state5["final_answer"]
    convo.add_assistant_message(state5["final_answer"])
    # Memory STILL preserved - we didn't lose Paris because of an OOS turn
    assert convo.prior_query.destination == "Paris"


# ---------------------------------------------------------------------------
# Clarifier path - extractor sets needs_clarification → branch into clarifier
# ---------------------------------------------------------------------------


def test_clarifier_path_when_origin_missing(substrate: AgentSubstrate, tmp_path: Path) -> None:
    """If the extractor flags missing_fields, the graph branches to clarifier."""
    convo = Conversation()
    agent = build_agent(substrate)
    client = substrate.client
    assert isinstance(client, MockClient)

    _register_router(client, Intent.FLIGHT_SEARCH)
    _register_extractor(
        client,
        FlightQuery(
            destination="Bali",
            departure_date=date(2026, 6, 1),
            needs_clarification=True,
            missing_fields=["origin"],
        ),
    )
    _register_clarifier(client, "What city are you flying from?")

    tracer = Tracer(turn_id="clarify", trace_dir=tmp_path / "traces", redact=False)
    convo.add_user_message("flights to Bali next month")
    state = agent.invoke(_make_state(convo.messages[-1].content, convo, tracer))

    assert state["flight_query"].needs_clarification is True
    assert state["final_answer"] == "What city are you flying from?"
    nodes = [e.node for e in tracer.events]
    # Path: router → extractor → clarifier (NOT flight_search)
    assert "clarifier" in nodes
    assert "flight_search" not in nodes


# ---------------------------------------------------------------------------
# Real-OpenAI smoke (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY") or os.environ.get("SKIP_OPENAI_INTEGRATION") == "1",
    reason="No OPENAI_API_KEY or explicitly skipped",
)
def test_full_agent_real_openai_smoke(tmp_path: Path) -> None:
    """One real round-trip through the agent against live OpenAI."""
    from app.graph.builder import default_substrate

    sub = default_substrate(self_critique=False)
    # Override KB cache dir to keep the test hermetic
    sub.kb = KBRetriever(cache_dir=tmp_path / ".faiss")

    agent = build_agent(sub)
    convo = Conversation()
    tracer = Tracer(turn_id="live-smoke", trace_dir=tmp_path / "traces", redact=True)
    convo.add_user_message("Find me a flight from Dubai to Tokyo in August")
    state = agent.invoke(
        {
            "user_message": convo.messages[-1].content,
            "summary": convo.summary(),
            "prior_query": None,
            "tracer": tracer,
            "turn_id": tracer.turn_id,
        }
    )
    assert state.get("final_answer"), "agent must produce a final reply"
