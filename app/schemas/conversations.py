"""Structured memory; identity cannot be embedded in a state update."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: Literal["awaiting_details", "investigating", "answered", "awaiting_review"] = "awaiting_details"
    pending_fields: list[Literal["trip_id", "dispute_date", "dispute_type", "claimed_amount"]] = Field(default_factory=list)
    focused_dispute_id: str | None = None
    summary: str = Field(default="", max_length=8192)
    last_answer: str = Field(default="", max_length=8192)
    pushback_count: int = Field(default=0, ge=0, strict=True)
