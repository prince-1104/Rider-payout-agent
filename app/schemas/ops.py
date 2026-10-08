"""Schemas for ``GET /ops/pending`` (shape fixed by SUBMISSION.md)."""

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PendingItemType = Literal["approval", "escalation"]


class PendingItem(BaseModel):
    id: str
    rider_id: str
    type: PendingItemType
    amount: int | None = Field(default=None, description="Whole rupees; set for approvals, null for escalations.")
    reason: str
    created_at: datetime
    dispute_type: str | None = None
    evidence: dict = Field(default_factory=dict)
    dispute_id: str | None = None
    conversation_id: str | None = None


class ConversationSummary(BaseModel):
    conversation_id: str
    rider_id: str
    state: dict
    created_at: datetime
    updated_at: datetime
    active_dispute_count: int
    latest_message_preview: str | None = None
    latest_message_at: datetime | None = None


class OpsDispute(BaseModel):
    dispute_id: str
    rider_id: str
    conversation_id: str
    dispute_type: str
    trip_id: str | None
    dispute_date: date | None
    status: str
    expected_amount: int | None
    actual_amount: int | None
    amount_owed: int
    explanation: str
    evidence: dict
    is_active: bool
    created_at: datetime

    @classmethod
    def from_model(cls, dispute):
        return cls.model_validate({
            key: getattr(dispute, key) for key in (
                "dispute_id", "rider_id", "conversation_id", "dispute_type", "trip_id", "dispute_date",
                "status", "expected_amount", "actual_amount", "amount_owed", "explanation", "evidence",
                "is_active", "created_at",
            )
        } | {"created_at": dispute.created_at.replace(tzinfo=timezone.utc)
            if dispute.created_at.tzinfo is None else dispute.created_at})


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=1000)


class OpsDecisionReply(BaseModel):
    status: Literal["resolved", "rejected"]
    amount: int | None = None
    payout_id: str | None = None
    reply: str | None = None
