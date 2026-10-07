"""Schemas for the vendor webhook ``POST /messages``."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class InboundMessageIn(BaseModel):
    """A single rider message as POSTed by the messaging vendor."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    message_id: str = Field(min_length=1, max_length=128, description="Vendor message id (wamid).")
    rider_id: str = Field(
        min_length=1,
        max_length=32,
        description="Rider id resolved by the vendor from the sender's phone number.",
    )
    # Empty text is accepted: rejecting it with 4xx would only make the vendor retry forever.
    text: str = Field(max_length=4096)
    received_at: AwareDatetime = Field(description="Timezone-aware ISO-8601 timestamp.")


class MessageReply(BaseModel):
    """Response body. The vendor relays ``reply`` back to the rider verbatim."""

    reply: str
