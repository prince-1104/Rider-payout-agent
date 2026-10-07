"""Schemas for ``GET /trace/{rider_id}`` (shape fixed by SUBMISSION.md)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

TraceStepType = Literal["message_in", "tool_call", "decision", "reply", "error"]


class TraceStep(BaseModel):
    at: datetime
    type: TraceStepType
    name: str
    input: Any = None
    output: Any = None
