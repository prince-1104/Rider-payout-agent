"""Vendor webhook: one rider message in, one reply out."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.schemas.messages import InboundMessageIn, MessageReply
from app.services.idempotency import MessageIdentityConflict, record_reply, register_inbound_message
from app.services.conversation_service import ConversationService

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
    try:
        registration = register_inbound_message(session, payload)
    except MessageIdentityConflict as exc:
        raise HTTPException(status_code=409, detail="Message ID conflicts with the request identity") from exc

    if registration.is_duplicate:
        logger.info("duplicate delivery message_id=%s rider_id=%s", payload.message_id, payload.rider_id)
        stored = registration.message.reply
        return MessageReply(reply=stored if stored is not None else PLACEHOLDER_REPLY)

    reply = PLACEHOLDER_REPLY
    conversations = ConversationService(session)
    conversation = conversations.get_or_create_conversation(payload.rider_id)
    conversations.append_message(payload.rider_id, conversation_id=conversation.conversation_id,
                                 message_id=payload.message_id, text=payload.text,
                                 received_at=payload.received_at)
    conversations.append_message(payload.rider_id, conversation_id=conversation.conversation_id,
                                 message_id=payload.message_id, text=reply,
                                 received_at=payload.received_at, role="assistant")
    record_reply(session, registration.message, reply)
    return MessageReply(reply=reply)
