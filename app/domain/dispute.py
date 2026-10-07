"""Structured, immutable claims and investigation results. No text interpretation."""

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.domain.dispute_types import DisputeStatus, DisputeType


class DisputeRequest(BaseModel):
    """Untrusted claim details; identity is supplied separately by the caller.

    ``rider_text`` is retained for the caller's context only and is never parsed
    or treated as evidence. No client-supplied expected/actual/owed amounts are
    accepted. A claimed amount is descriptive, never a financial authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    dispute_id: str | None = Field(default=None, min_length=1, max_length=128)
    dispute_type: DisputeType = DisputeType.GENERAL_PAYOUT_DISPUTE
    trip_id: str | None = Field(default=None, min_length=1, max_length=32)
    dispute_date: date | None = None
    claimed_amount: StrictInt | None = Field(default=None, ge=0)
    rider_text: str = ""

    @field_validator("dispute_type", mode="before")
    @classmethod
    def unknown_type(cls, value: Any) -> DisputeType:
        try:
            return DisputeType(value)
        except (ValueError, TypeError):
            return DisputeType.UNKNOWN


class Dispute(BaseModel):
    """An investigation, not an authorization to transfer funds.

    Amounts are whole rupees. Expected/actual amounts are signed (penalties are
    negative), while amount_owed is always nonnegative. Unknown amounts are null.
    Payment history, duplicate claims and approvals must be checked by a future
    settlement service before issuing money.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dispute_id: str
    rider_id: str
    dispute_type: DisputeType
    trip_id: str | None
    dispute_date: date | None
    claimed_amount: StrictInt | None
    expected_amount: StrictInt | None = None
    actual_amount: StrictInt | None = None
    amount_owed: StrictInt = Field(default=0, ge=0)
    status: DisputeStatus
    explanation: str
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enforce_shortfall(self) -> "Dispute":
        if self.amount_owed:
            if self.status != DisputeStatus.VALID:
                raise ValueError("Only valid disputes can have an amount owed")
            if self.expected_amount is None or self.actual_amount is None:
                raise ValueError("Amounts require verified expected and actual values")
            if self.amount_owed > max(0, self.expected_amount - self.actual_amount):
                raise ValueError("Amount owed exceeds the verified shortfall")
        if self.status == DisputeStatus.VALID and self.amount_owed == 0:
            raise ValueError("A valid underpayment dispute must have a positive shortfall")
        return self
