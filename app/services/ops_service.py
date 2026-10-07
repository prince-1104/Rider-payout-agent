"""Application-only queueing, with amounts derived from persisted engine results."""

from datetime import timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.db.models import Conversation, OpsQueueItem, StoredDispute
from app.domain.dispute_types import DisputeStatus
from app.schemas.ops import PendingItem


class OpsService:
    def __init__(self, session):
        self.session = session

    def enqueue(self, rider_id, conversation_id, *, dispute_id, message_id, kind, reason):
        conversation = self.session.get(Conversation, conversation_id)
        if conversation is None or conversation.rider_id != rider_id:
            raise ValueError("Conversation not found for authenticated rider")
        dispute = self.session.get(StoredDispute, dispute_id) if dispute_id else None
        if dispute_id and (dispute is None or dispute.rider_id != rider_id or dispute.conversation_id != conversation_id):
            raise ValueError("Dispute not found for authenticated conversation")
        if kind not in {"approval", "escalation"}:
            raise ValueError("Unsupported ops work type")
        amount = None
        if kind == "approval":
            if dispute is None or dispute.status != DisputeStatus.VALID.value or dispute.amount_owed <= 0:
                raise ValueError("Approval requires a verified shortfall")
            amount = dispute.amount_owed
        identifier = str(uuid5(NAMESPACE_URL, f"quickdrop:ops:{rider_id}:{dispute_id or 'message:' + message_id}"))
        item = self.session.get(OpsQueueItem, identifier)
        if item is None:
            item = OpsQueueItem(id=identifier, rider_id=rider_id, conversation_id=conversation_id,
                                dispute_id=dispute_id, type=kind, amount=amount, reason=reason)
            self.session.add(item)
        else:
            item.type, item.amount, item.reason = kind, amount, reason
            item.status = "pending"
        self.session.flush()
        return {"ops_item_id": item.id, "type": item.type, "amount": item.amount}

    def mark_resolved(self, rider_id, dispute_id):
        items = self.session.scalars(select(OpsQueueItem).where(
            OpsQueueItem.rider_id == rider_id, OpsQueueItem.dispute_id == dispute_id)).all()
        for item in items:
            item.status = "resolved"

    def list_pending(self):
        items = self.session.scalars(select(OpsQueueItem).where(OpsQueueItem.status == "pending")
                                     .order_by(OpsQueueItem.created_at, OpsQueueItem.id)).all()
        return [PendingItem(id=i.id, rider_id=i.rider_id, type=i.type, amount=i.amount,
                            reason=i.reason, created_at=i.created_at.replace(tzinfo=timezone.utc)
                            if i.created_at.tzinfo is None else i.created_at) for i in items]
