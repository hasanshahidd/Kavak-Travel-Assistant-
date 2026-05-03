"""Schema for the LLM-driven out-of-scope responder.

Replaces the deterministic regex-and-whitelist branching that v2 used.
The LLM now writes the user-facing reply AND classifies the off-domain
message into a sub-category so the UI badge stays informative.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OOSReply(BaseModel):
    """One LLM-generated reply for an out-of-scope user message.

    Three sub-categories drive the badge in the Streamlit sidebar:
    greeting (social ack), info (capabilities / meta query), redirect
    (everything else off-domain).
    """

    model_config = ConfigDict(extra="forbid")

    category: Literal["greeting", "info", "redirect"] = Field(
        ...,
        description="Sub-classification of the off-domain message; drives UI badge.",
    )
    reply: str = Field(
        ...,
        min_length=1,
        max_length=600,
        description="User-facing plain-text reply. Max 2 sentences per the prompt.",
    )
