"""Pydantic v2 contracts - strict typed boundaries between layers."""

from app.schemas.chat import ChatMessage, ChatRequest, ChatResponse, TraceEvent
from app.schemas.flight import (
    Flight,
    FlightQuery,
    FlightResult,
    ResponseCritique,
    SearchOutcome,
    TripType,
)
from app.schemas.intent import Intent, RouterOutput
from app.schemas.oos import OOSReply
from app.schemas.rag import Chunk, Citation, RagAnswer

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "Chunk",
    "Citation",
    "Flight",
    "FlightQuery",
    "FlightResult",
    "Intent",
    "OOSReply",
    "RagAnswer",
    "ResponseCritique",
    "RouterOutput",
    "SearchOutcome",
    "TraceEvent",
    "TripType",
]
