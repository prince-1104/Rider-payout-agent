"""Vendor webhook: one rider message in, one reply out."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.schemas.messages import InboundMessageIn, MessageReply
from app.services.idempotency import record_reply, register_inbound_message

logger = logging.getLogger(__name__)

router = APIRouter(tags=["messages"])

# Phase 1 placeholder; replaced by the agent loop in a later phase.
PLACEHOLDER_REPLY = (
    "Thanks, we have received your message. We are checking your payout and will get back to you shortly."
)


@router.post("/messages", response_model=MessageReply)
def receive_message(
    payload: InboundMessageIn,
    session: Annotated[Session, Depends(get_session)],
) -> MessageReply:
    registration = register_inbound_message(session, payload)

    if registration.is_duplicate:
        logger.info("duplicate delivery message_id=%s rider_id=%s", payload.message_id, payload.rider_id)
        stored = registration.message.reply
        return MessageReply(reply=stored if stored is not None else PLACEHOLDER_REPLY)

    reply = PLACEHOLDER_REPLY
    record_reply(session, registration.message, reply)
    return MessageReply(reply=reply)
