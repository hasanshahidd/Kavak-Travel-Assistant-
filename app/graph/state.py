"""Agent state shape — what flows through the LangGraph state machine.

Designed to be:

* **Total-False TypedDict** — every key is optional so nodes can update
  only the fields they own. LangGraph merges partial updates into the
  running state automatically.
* **Pydantic models, not raw dicts** — the heavy domain values
  (``flight_query``, ``flight_results``, ``rag_answer``) keep their
  Pydantic types. The state is a *carrier*; the typed contracts live
  inside it.
* **Trace-aware** — the state holds the live :class:`Tracer`, so any
  node that wants to emit an event has access without sneaking through
  globals.

Reading this file plus ``graph/builder.py`` should give a reviewer the
complete agent topology in two screens.
"""

from __future__ import annotations

from typing import TypedDict

from app.llm.tracing import Tracer
from app.schemas.chat import ChatMessage
from app.schemas.flight import FlightQuery, SearchOutcome
from app.schemas.intent import Intent
from app.schemas.rag import Chunk, RagAnswer


class AgentState(TypedDict, total=False):
    """The state object LangGraph threads through every node.

    All keys are optional. Each node updates only the fields it owns:

    * ``router`` writes ``intent``
    * ``extractor`` writes ``flight_query``
    * ``flight_search`` writes ``flight_results``
    * ``retriever`` writes ``retrieved_chunks``
    * ``answerer`` writes ``rag_answer``
    * ``responder`` / ``clarifier`` / ``out_of_scope`` write ``final_answer``
    """

    # --- Conversation context (populated by the harness, not by graph nodes) ---
    user_message: str
    history: list[ChatMessage]
    summary: str
    prior_query: FlightQuery | None  # last extractor output, used for memory-merge

    # --- Per-turn metadata ---
    turn_id: str
    tracer: Tracer

    # --- Routing ---
    intent: Intent

    # --- Flight path ---
    flight_query: FlightQuery | None
    flight_results: SearchOutcome | None

    # --- RAG path ---
    retrieved_chunks: list[Chunk]
    rag_answer: RagAnswer | None

    # --- Output ---
    final_answer: str


__all__ = ["AgentState"]
