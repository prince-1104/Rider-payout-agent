"""Shared application state and narrow, untrusted model output contracts."""

from datetime import date
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from app.domain.dispute import Dispute, DisputeRequest
from app.domain.dispute_types import DisputeType
from app.schemas.trace import TraceStep


class StructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractedClaim(StructuredOutput):
    """Only claim references, never identity, dispatch facts or financial amounts."""

    existing_dispute_id: str | None = Field(default=None, min_length=1, max_length=128)
    dispute_type: DisputeType
    trip_id: str | None = Field(default=None, min_length=1, max_length=32)
    dispute_date: date | None = None


class ConversationOutput(StructuredOutput):
    claims: list[ExtractedClaim] = Field(max_length=5)
    needs_information: StrictBool
    multiple_disputes: StrictBool

    @model_validator(mode="after")
    def check_multiple(self):
        if self.multiple_disputes != (len(self.claims) > 1):
            raise ValueError("Multiple-dispute flag must match extracted claims")
        return self


class EvidencePath(str, Enum):
    TRIP = "trip"
    DAY = "day"
    INCENTIVE = "incentive"
    PENALTY = "penalty"


class InvestigationJob(StructuredOutput):
    claim_index: StrictInt = Field(ge=0, lt=5)
    path: EvidencePath


class InvestigationPlan(StructuredOutput):
    jobs: list[InvestigationJob] = Field(max_length=5)


class InvestigationResult(StructuredOutput):
    """Constructed by application code from actual tool responses, never by an LLM."""

    dispute_id: str
    evidence: dict[str, Any]


class Action(str, Enum):
    ASK_FOR_INFO = "ASK_FOR_INFO"
    NO_DISPUTE = "NO_DISPUTE"
    AUTO_PAY = "AUTO_PAY"
    OPS_APPROVAL = "OPS_APPROVAL"
    ESCALATION = "ESCALATION"


class PolicyDecision(StructuredOutput):
    """Only the deterministic policy engine constructs this financial authority."""

    dispute_id: str | None
    action: Action
    amount: StrictInt = Field(default=0, ge=0)
    reason: str


class ResolutionRecommendation(StructuredOutput):
    dispute_id: str | None
    action: Action
    explanation: str = Field(max_length=1000)


class ResolutionOutput(StructuredOutput):
    recommendations: list[ResolutionRecommendation] = Field(max_length=5)


class HistoryTurn(StructuredOutput):
    role: Literal["rider", "assistant"]
    text: str


class AgentState(BaseModel):
    """State belongs to the application; models receive serialized views only."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    rider_id: str = Field(frozen=True)
    conversation_id: str = Field(frozen=True)
    latest_message: str = Field(frozen=True)
    conversation_history: list[HistoryTurn] = Field(default_factory=list)
    claims: list[DisputeRequest] = Field(default_factory=list)
    investigation_results: list[InvestigationResult] = Field(default_factory=list)
    validated_disputes: list[Dispute] = Field(default_factory=list)
    policy_decision: list[PolicyDecision] = Field(default_factory=list)
    reply: str = ""
    trace: list[TraceStep] = Field(default_factory=list)
    agent_iterations: int = 0
    execution_results: list[dict[str, Any]] = Field(default_factory=list)
    security_flags: list[str] = Field(default_factory=list)
