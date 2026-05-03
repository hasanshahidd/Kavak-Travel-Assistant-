"""Intent classification - output of the router node.

Kept deliberately small: a single enum + a thin wrapper so the router prompt
can target a tiny JSON shape (1-token classifications are fast and cheap).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Intent(StrEnum):
    """High-level user intent."""

    FLIGHT_SEARCH = "flight_search"
    POLICY_QA = "policy_qa"
    CLARIFY = "clarify"
    OUT_OF_SCOPE = "out_of_scope"


class RouterOutput(BaseModel):
    """Strict router contract - model returns exactly this shape."""

    intent: Intent
    rationale: str = Field(
        ...,
        description="One-sentence justification for the chosen intent. Used in trace logs.",
        max_length=200,
    )
