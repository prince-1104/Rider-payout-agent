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
            OpsQueueItem.rider_id == rider_id, OpsQueueItem.dispute_id == dispute_id,
            OpsQueueItem.status == "pending")).all()
        for item in items:
            item.status = "resolved"

    def mark_rejected(self, item, reason):
        if item.status != "pending":
            raise ValueError("Ops item is no longer pending")
        item.status = "rejected"
        item.resolution_reason = reason
        self.session.flush()

    def get_item(self, item_id, *, pending_only=False):
        query = select(OpsQueueItem).where(OpsQueueItem.id == item_id)
        if pending_only:
            query = query.where(OpsQueueItem.status == "pending")
        return self.session.scalar(query.with_for_update())

    def get_pending_dispute_item(self, dispute_id):
        return self.session.scalar(select(OpsQueueItem).where(
            OpsQueueItem.dispute_id == dispute_id, OpsQueueItem.status == "pending",
        ).order_by(OpsQueueItem.created_at, OpsQueueItem.id).with_for_update())

    def list_disputes(self):
        return list(self.session.scalars(select(StoredDispute).order_by(
            StoredDispute.created_at.desc(), StoredDispute.dispute_id)))

    def get_dispute(self, dispute_id):
        return self.session.get(StoredDispute, dispute_id)

    def list_pending(self):
        items = self.session.scalars(select(OpsQueueItem).where(OpsQueueItem.status == "pending")
                                     .order_by(OpsQueueItem.created_at, OpsQueueItem.id)).all()
        result = []
        for item in items:
            dispute = self.session.get(StoredDispute, item.dispute_id) if item.dispute_id else None
            result.append(PendingItem(
                id=item.id, rider_id=item.rider_id, type=item.type, amount=item.amount,
                reason=item.reason,
                created_at=item.created_at.replace(tzinfo=timezone.utc)
                if item.created_at.tzinfo is None else item.created_at,
                dispute_type=dispute.dispute_type if dispute else None,
                evidence=dispute.evidence if dispute else {},
                dispute_id=item.dispute_id, conversation_id=item.conversation_id,
            ))
        return result
