"""Schemas for ``GET /ops/pending`` (shape fixed by SUBMISSION.md)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

PendingItemType = Literal["approval", "escalation"]


class PendingItem(BaseModel):
    id: str
    rider_id: str
    type: PendingItemType
    amount: int | None = Field(default=None, description="Whole rupees; set for approvals, null for escalations.")
    reason: str
    created_at: datetime
