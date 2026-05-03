"""Conversation DTOs - request / response / trace shapes.

Used at the boundary between the UI layer (``main.py`` / ``streamlit_app.py``)
and the agent. Kept distinct from internal domain schemas so the public
surface can evolve independently of agent state shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.flight import Flight
from app.schemas.intent import Intent
from app.schemas.rag import RagAnswer


class ChatMessage(BaseModel):
    """A single message in the conversation history.

    Roles are ``user`` for inbound text and ``assistant`` for the agent's
    final reply. Internal node outputs are NOT messages - they live in the
    trace.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = Field(
        ..., description="Who produced this message."
    )
    content: str = Field(..., min_length=1)
    timestamp: datetime = Field(..., description="When the message was added to memory.")


class ChatRequest(BaseModel):
    """Inbound - a single user message."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=2000)
    session_id: str = Field(
        ...,
        description="Client-generated UUID; ties memory + trace events together.",
        min_length=8,
        max_length=64,
    )


class ChatResponse(BaseModel):
    """Outbound - the user-facing reply plus structured side-channels."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    turn_id: str = Field(
        ...,
        description="Per-turn id; used to fetch the trace via /api/trace/{turn_id}.",
    )
    reply: str = Field(..., min_length=1, description="The user-facing markdown reply.")
    intent: Intent
    flights: list[Flight] | None = None
    rag: RagAnswer | None = None
    needs_clarification: bool = False


class TraceEvent(BaseModel):
    """One LLM/tool call in the agent's trace.

    Streamed to the frontend's trace sidebar so the reviewer sees every
    reasoning step: which prompt fired, latency, token counts, output.
    """

    model_config = ConfigDict(extra="forbid")

    turn_id: str
    timestamp: datetime
    node: str = Field(..., description="Graph node name, e.g. 'extractor'.")
    prompt_id: str | None = Field(
        None, description="Prompt id from frontmatter, e.g. 'extractor.v3'."
    )
    prompt_hash: str | None = Field(
        None, description="SHA-256 of prompt body - ties trace to exact wording."
    )
    latency_ms: float = Field(..., ge=0)
    tokens_in: int = Field(0, ge=0)
    tokens_out: int = Field(0, ge=0)
    cost_usd: float = Field(0.0, ge=0)
    output: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured node output (extracted filters, retrieved chunks, etc.).",
    )
