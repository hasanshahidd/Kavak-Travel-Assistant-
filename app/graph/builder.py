"""LangGraph state-machine assembly.

Keeps the graph topology in one screen so a reviewer can trace any user
query through the agent without hopping between files.

Topology:

    START
      │
      ▼
    router ──┬─► flight_search (extractor → conditional → flight_search/clarifier)
             ├─► policy_qa (retriever → answerer)
             ├─► clarify (clarifier)
             └─► out_of_scope (canned reply)

The graph is built around dependency injection: ``build_agent()`` accepts
the substrate (``LLMClient``, ``KBRetriever``, ``FlightIndex``) so tests
can inject mocks without monkeypatching. The same factory is used by
``main.py`` and ``streamlit_app.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.answerer import answer as _answer
from app.graph.nodes.clarifier import clarify as _clarify
from app.graph.nodes.extractor import extract as _extract
from app.graph.nodes.flight_search import search_flights as _search_flights
from app.graph.nodes.out_of_scope import out_of_scope_reply
from app.graph.nodes.responder import respond as _respond
from app.graph.nodes.retriever import retrieve as _retrieve
from app.graph.nodes.router import route as _route
from app.graph.state import AgentState
from app.llm.client import LLMClient, get_llm_client
from app.schemas.intent import Intent
from app.tools.data_inventory import flight_inventory, kb_inventory
from app.tools.flight_index import FlightIndex
from app.tools.kb_retriever import KBRetriever

# ---------------------------------------------------------------------------
# Substrate - the IO-bound dependencies shared across nodes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentSubstrate:
    """The IO surfaces the graph nodes need.

    A single substrate instance is created once and bound into every node
    closure, so each turn reuses the same FAISS index, in-memory flight
    catalogue, and LLM connection.
    """

    client: LLMClient
    kb: KBRetriever
    index: FlightIndex
    self_critique: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def default_substrate(self_critique: bool = False) -> AgentSubstrate:
    """Build a substrate with provider-driven defaults. Used by entry points."""
    return AgentSubstrate(
        client=get_llm_client(),
        kb=KBRetriever(),
        index=FlightIndex(),
        self_critique=self_critique,
    )


# ---------------------------------------------------------------------------
# Node adapters - bind substrate, hand LangGraph the (state) -> dict shape
# ---------------------------------------------------------------------------

# A LangGraph node is a callable that takes the running state and returns a
# partial state-update dict. Each `_*_node` factory closes over the substrate
# (LLM client, FAISS retriever, flight index) and returns one such callable.
NodeFn = Callable[[AgentState], dict[str, Any]]


def _router_node(substrate: AgentSubstrate) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        out = _route(
            user_message=state["user_message"],
            conversation_summary=state.get("summary", "(no prior conversation)"),
            client=substrate.client,
            tracer=state.get("tracer"),
        )
        return {"intent": out.intent}

    return node


def _extractor_node(substrate: AgentSubstrate) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        merged = _extract(
            user_message=state["user_message"],
            conversation_summary=state.get("summary", "(no prior conversation)"),
            prior_query=state.get("prior_query"),
            client=substrate.client,
            tracer=state.get("tracer"),
        )
        return {"flight_query": merged}

    return node


def _flight_search_node(substrate: AgentSubstrate) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        query = state.get("flight_query")
        if query is None:
            return {"flight_results": None}
        outcome = _search_flights(
            query=query,
            index=substrate.index,
            tracer=state.get("tracer"),
        )
        return {"flight_results": outcome}

    return node


def _retriever_node(substrate: AgentSubstrate) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        chunks = _retrieve(
            question=state["user_message"],
            kb=substrate.kb,
            conversation_summary=state.get("summary"),
            tracer=state.get("tracer"),
        )
        return {"retrieved_chunks": chunks}

    return node


def _answerer_node(substrate: AgentSubstrate) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        result = _answer(
            question=state["user_message"],
            chunks=state.get("retrieved_chunks", []),
            client=substrate.client,
            conversation_summary=state.get("summary"),
            tracer=state.get("tracer"),
        )
        return {"rag_answer": result, "final_answer": result.answer}

    return node


def _responder_node(substrate: AgentSubstrate) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        query = state.get("flight_query")
        outcome = state.get("flight_results")
        if query is None or outcome is None:
            return {"final_answer": "I wasn't able to run a flight search."}
        text = _respond(
            query=query,
            outcome=outcome,
            client=substrate.client,
            tracer=state.get("tracer"),
            self_critique=substrate.self_critique,
        )
        return {"final_answer": text}

    return node


def _clarifier_node(substrate: AgentSubstrate) -> NodeFn:
    def node(state: AgentState) -> dict[str, Any]:
        query = state.get("flight_query")
        missing = list(query.missing_fields) if query is not None else []
        text = _clarify(
            user_message=state["user_message"],
            conversation_summary=state.get("summary", "(no prior conversation)"),
            missing_fields=missing,
            client=substrate.client,
            tracer=state.get("tracer"),
        )
        # Reflect the actual node that ran in the badge. The router may have
        # initially predicted FLIGHT_SEARCH, but if the extractor flagged
        # `needs_clarification` and we ended up here, the user-facing badge
        # should say CLARIFY, not "flight search".
        return {"final_answer": text, "intent": Intent.CLARIFY}

    return node


def _out_of_scope_node(substrate: AgentSubstrate) -> NodeFn:
    # Inventory is composed from already-loaded data (FlightIndex + KB chunk
    # metadata). Cache it on the closure: the catalogue and KB are immutable
    # within a process, so recomputing every turn would be pointless work
    # without changing the answer.
    flight_summary: str | None = None
    kb_summary: str | None = None

    def node(state: AgentState) -> dict[str, Any]:
        nonlocal flight_summary, kb_summary
        if flight_summary is None:
            flight_summary = flight_inventory(substrate.index)
        if kb_summary is None:
            kb_summary = kb_inventory(substrate.kb)
        result = out_of_scope_reply(
            user_message=state["user_message"],
            client=substrate.client,
            flight_inventory=flight_summary,
            kb_inventory=kb_summary,
            tracer=state.get("tracer"),
        )
        return {"final_answer": result.reply}

    return node


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------


def _branch_on_intent(state: AgentState) -> str:
    intent = state.get("intent")
    if intent is Intent.FLIGHT_SEARCH:
        return "extractor"
    if intent is Intent.POLICY_QA:
        return "retriever"
    if intent is Intent.CLARIFY:
        return "clarifier"
    return "out_of_scope"


def _branch_after_extract(state: AgentState) -> str:
    query = state.get("flight_query")
    if query is None or query.needs_clarification:
        return "clarifier"
    return "flight_search"


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_agent(substrate: AgentSubstrate | None = None) -> Any:
    """Compile the LangGraph agent. Returns a callable ``CompiledGraph``."""
    sub = substrate or default_substrate()

    g: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)

    g.add_node("router", _router_node(sub))  # type: ignore[call-overload]
    g.add_node("extractor", _extractor_node(sub))  # type: ignore[call-overload]
    g.add_node("flight_search", _flight_search_node(sub))  # type: ignore[call-overload]
    g.add_node("responder", _responder_node(sub))  # type: ignore[call-overload]
    g.add_node("retriever", _retriever_node(sub))  # type: ignore[call-overload]
    g.add_node("answerer", _answerer_node(sub))  # type: ignore[call-overload]
    g.add_node("clarifier", _clarifier_node(sub))  # type: ignore[call-overload]
    g.add_node("out_of_scope", _out_of_scope_node(sub))  # type: ignore[call-overload]

    g.add_edge(START, "router")
    g.add_conditional_edges(
        "router",
        _branch_on_intent,
        {
            "extractor": "extractor",
            "retriever": "retriever",
            "clarifier": "clarifier",
            "out_of_scope": "out_of_scope",
        },
    )
    g.add_conditional_edges(
        "extractor",
        _branch_after_extract,
        {"flight_search": "flight_search", "clarifier": "clarifier"},
    )
    g.add_edge("flight_search", "responder")
    g.add_edge("retriever", "answerer")

    # Terminal nodes
    for terminal in ("responder", "answerer", "clarifier", "out_of_scope"):
        g.add_edge(terminal, END)

    return g.compile()


__all__ = [
    "AgentSubstrate",
    "build_agent",
    "default_substrate",
]
