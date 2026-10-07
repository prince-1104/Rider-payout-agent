"""Internal ops queue, dispute review and server-authorized decisions."""

from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import get_session
from app.db.models import Conversation, OpsQueueItem, StoredDispute
from app.schemas.conversations import ConversationState
from app.schemas.ops import OpsDecisionReply, OpsDispute, PendingItem, RejectRequest
from app.services.conversation_service import ConversationService
from app.services.ops_service import OpsService
from app.services.payment_service import PaymentPolicyDenied, PaymentService
from app.services.trace import TraceRecorder
from app.integrations.payswift import PaySwiftClient

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/pending", response_model=list[PendingItem])
def list_pending(session: Annotated[Session, Depends(get_session)]) -> list[PendingItem]:
    return OpsService(session).list_pending()


@router.get("/disputes", response_model=list[OpsDispute])
def list_disputes(session: Annotated[Session, Depends(get_session)]) -> list[OpsDispute]:
    return [OpsDispute.from_model(dispute) for dispute in OpsService(session).list_disputes()]


@router.get("/disputes/{dispute_id}", response_model=OpsDispute)
def get_dispute(dispute_id: str, session: Annotated[Session, Depends(get_session)]) -> OpsDispute:
    service = OpsService(session)
    dispute = service.get_dispute(dispute_id)
    if dispute is None:
        queue_item = service.get_item(dispute_id)
        if queue_item is not None and queue_item.dispute_id:
            dispute = service.get_dispute(queue_item.dispute_id)
    if dispute is None:
        raise HTTPException(status_code=404, detail="Dispute not found")
    return OpsDispute.from_model(dispute)


@router.post("/disputes/{dispute_id}/approve", response_model=OpsDecisionReply)
def approve_dispute(dispute_id: str, request: Request,
                    session: Annotated[Session, Depends(get_session)]) -> OpsDecisionReply:
    service = OpsService(session)
    queue = service.get_pending_dispute_item(dispute_id)
    if queue is None:
        queue = service.get_item(dispute_id, pending_only=True)
        if queue is not None and queue.dispute_id:
            dispute_id = queue.dispute_id
    if queue is None or queue.type != "approval":
        raise HTTPException(status_code=404, detail="Pending approval not found")
    rider_id = queue.rider_id
    session.commit()
    factory = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)
    options = request.app.state.message_workflow_options
    processor = PaymentService(factory,
        payswift_factory=options.get("payswift_factory", PaySwiftClient),
        clock=options.get("clock", lambda: datetime.now(timezone.utc)))
    try:
        result = processor.approve_ops_dispute(rider_id, dispute_id)
    except PaymentPolicyDenied as exc:
        raise HTTPException(status_code=409, detail="Approval could not be safely executed; review remains required") from exc
    if result.get("action") == "ESCALATION":
        raise HTTPException(status_code=409, detail="Current evidence requires reconciliation; item was escalated")
    payment = result.get("payment") or {}
    return OpsDecisionReply(status="resolved", amount=payment.get("amount", 0), payout_id=payment.get("payout_id"))


@router.post("/disputes/{dispute_id}/reject", response_model=OpsDecisionReply)
def reject_dispute(dispute_id: str, payload: RejectRequest,
                   session: Annotated[Session, Depends(get_session)]) -> OpsDecisionReply:
    service = OpsService(session)
    item = service.get_pending_dispute_item(dispute_id)
    if item is None:
        item = service.get_item(dispute_id, pending_only=True)
    if item is None:
        raise HTTPException(status_code=404, detail="Pending ops item not found")
    trace = TraceRecorder(sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False), item.rider_id)
    invocation = trace.start("ops_rejection", {"ops_item_id": item.id, "type": item.type}, kind="decision")
    rider_reply = None
    try:
        service.mark_rejected(item, payload.reason)
        if item.dispute_id:
            dispute = session.scalar(select(StoredDispute).where(
                StoredDispute.dispute_id == item.dispute_id,
                StoredDispute.rider_id == item.rider_id).with_for_update())
        else:
            dispute = None
        conversation = session.scalar(select(Conversation).where(
            Conversation.conversation_id == item.conversation_id,
            Conversation.rider_id == item.rider_id).with_for_update())
        active = conversation is not None and conversation.state.get("phase") in {
            "awaiting_review", "awaiting_details", "investigating",
        }
        if active:
            rider_reply = ("Our operations team reviewed your payout concern and could not approve a payment. "
                           "Please contact support if you have additional information.")
            conversations = ConversationService(session)
            if dispute is not None and dispute.is_active:
                conversations.resolve_dispute(item.rider_id, conversation_id=item.conversation_id,
                    dispute_id=dispute.dispute_id, expected_revision=dispute.revision)
            conversations.append_message(item.rider_id, conversation_id=item.conversation_id,
                message_id=f"ops-rejection:{item.id}", text=rider_reply,
                received_at=datetime.now(timezone.utc), role="assistant")
            current = conversations._conversation(item.rider_id, item.conversation_id)
            prior_state = ConversationState.model_validate(current.state)
            conversations.update_conversation_state(item.rider_id, conversation_id=item.conversation_id,
                state=prior_state.model_copy(update={"phase": "answered", "last_answer": rider_reply,
                    "focused_dispute_id": dispute.dispute_id if dispute is not None
                    else prior_state.focused_dispute_id}),
                expected_version=current.version)
        session.commit()
        trace.finish(invocation, output={"status": "rejected", "reply_generated": rider_reply is not None})
    except Exception as exc:
        session.rollback()
        trace.finish(invocation, error=exc)
        raise
    return OpsDecisionReply(status="rejected", reply=rider_reply)
