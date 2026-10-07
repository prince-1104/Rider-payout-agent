"""Vendor webhook: durable, idempotent rider-message orchestration."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import get_session
from app.schemas.messages import InboundMessageIn, MessageReply
from app.services.idempotency import MessageIdentityConflict, record_reply
from app.services.message_workflow import MessageInProgress, MessageWorkflow

router = APIRouter(tags=["messages"])

@router.post("/messages", response_model=MessageReply,
             responses={503: {"description": "Delivery is unfinished; retry after the indicated delay"}})
def receive_message(
    payload: InboundMessageIn,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> MessageReply:
    # The ingress vendor resolves the sender identity; text/model outputs never do.
    # Fresh sessions give each stage a short, independent durable transaction.
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    workflow = MessageWorkflow(factory, **request.app.state.message_workflow_options)
    try:
        reply, prepared = workflow.prepare(payload)
        if reply is None:
            reply = workflow.execute(prepared, cache_reply=record_reply)
    except MessageIdentityConflict as exc:
        raise HTTPException(status_code=409, detail="Message ID conflicts with the request identity") from exc
    except MessageInProgress as exc:
        # Do not acknowledge unfinished work with 2xx: the vendor must retry.
        # A race with completion can immediately return the exact cached result.
        reply, _ = workflow.prepare(payload)
        if reply is None:
            raise HTTPException(status_code=503, detail="Message processing is in progress; retry shortly",
                                headers={"Retry-After": "1"}) from exc
    return MessageReply(reply=reply)
