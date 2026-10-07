"""Transactional conversation memory, independent of natural-language interpretation.

All methods require the authoritative request rider ID. No method infers identity
or structured claims from message text. Methods flush but never commit: callers
choose durable transaction boundaries for each workflow stage.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Conversation, ConversationMessage, StoredDispute
from app.db.database import begin_savepoint
from app.domain.dispute import Dispute, DisputeRequest
from app.schemas.conversations import ConversationState


class ConversationAccessError(ValueError):
    """Requested memory does not belong to the authoritative rider."""


class ConversationConflictError(ValueError):
    """A stale state/dispute revision would overwrite newer work."""


class ConversationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _rider(rider_id: str) -> str:
        if not isinstance(rider_id, str) or not rider_id.strip() or len(rider_id) > 32:
            raise ValueError("An authoritative rider_id of 1–32 characters is required")
        return rider_id.strip()

    def _conversation(self, rider_id: str, conversation_id: str) -> Conversation:
        conversation = self.session.scalar(select(Conversation).where(
            Conversation.conversation_id == conversation_id, Conversation.rider_id == self._rider(rider_id),
        ))
        if conversation is None:
            raise ConversationAccessError("Conversation not found for this rider")
        return conversation

    def get_or_create_conversation(self, rider_id: str) -> Conversation:
        rider_id = self._rider(rider_id)
        conversation = self.session.scalar(select(Conversation).where(Conversation.rider_id == rider_id))
        if conversation is not None:
            return conversation
        try:
            with begin_savepoint(self.session):
                conversation = Conversation(conversation_id=str(uuid4()), rider_id=rider_id,
                                            state=ConversationState().model_dump(mode="json"))
                self.session.add(conversation)
                self.session.flush()
        except IntegrityError:
            conversation = self.session.scalar(select(Conversation).where(Conversation.rider_id == rider_id))
            if conversation is None:
                raise
        return conversation

    def append_message(
        self, rider_id: str, *, conversation_id: str, message_id: str,
        text: str, received_at: datetime, role: str = "rider",
    ) -> ConversationMessage:
        conversation = self._conversation(rider_id, conversation_id)
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 128:
            raise ValueError("message_id must have 1–128 characters")
        if role not in {"rider", "assistant"}:
            raise ValueError("role must be rider or assistant")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")

        def existing():
            return self.session.scalar(select(ConversationMessage).where(
                ConversationMessage.message_id == message_id, ConversationMessage.role == role,
            ))

        def verify(message):
            if message.conversation_id != conversation_id or message.rider_id != conversation.rider_id:
                raise ConversationAccessError("Message ID belongs to another rider's conversation")
            return message

        message = existing()
        if message is not None:
            return verify(message)
        try:
            with begin_savepoint(self.session):
                # Atomic allocation serializes concurrent writers per conversation.
                sequence = self.session.scalar(update(Conversation).where(
                    Conversation.conversation_id == conversation_id,
                ).values(message_count=Conversation.message_count + 1, updated_at=func.now())
                    .returning(Conversation.message_count))
                message = ConversationMessage(
                    conversation_id=conversation_id, rider_id=conversation.rider_id,
                    message_id=message_id, role=role, sequence=sequence, text=text,
                    received_at=received_at.astimezone(timezone.utc),
                )
                self.session.add(message)
                self.session.flush()
        except IntegrityError:
            message = existing()
            if message is None:
                raise
            return verify(message)
        return message

    def get_conversation_history(self, rider_id: str, *, conversation_id: str) -> list[ConversationMessage]:
        self._conversation(rider_id, conversation_id)
        return list(self.session.scalars(select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation_id,
        ).order_by(ConversationMessage.sequence)))

    def get_active_disputes(self, rider_id: str, *, conversation_id: str) -> list[StoredDispute]:
        self._conversation(rider_id, conversation_id)
        return list(self.session.scalars(select(StoredDispute).where(
            StoredDispute.conversation_id == conversation_id, StoredDispute.is_active.is_(True),
        ).order_by(StoredDispute.created_at, StoredDispute.dispute_id)))

    def update_conversation_state(
        self, rider_id: str, *, conversation_id: str,
        state: ConversationState, expected_version: int,
    ) -> Conversation:
        conversation = self._conversation(rider_id, conversation_id)
        state = ConversationState.model_validate(state.model_dump() if isinstance(state, ConversationState) else state)
        if state.focused_dispute_id is not None:
            self._dispute(rider_id, conversation_id, state.focused_dispute_id)
        result = self.session.execute(update(Conversation).where(
            Conversation.conversation_id == conversation_id, Conversation.version == expected_version,
        ).values(state=state.model_dump(mode="json"), version=Conversation.version + 1,
                 updated_at=func.now()).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise ConversationConflictError("Conversation state changed; reload before updating")
        self.session.refresh(conversation)
        return conversation

    def _dispute(self, rider_id: str, conversation_id: str, dispute_id: str) -> StoredDispute:
        self._conversation(rider_id, conversation_id)
        dispute = self.session.scalar(select(StoredDispute).where(
            StoredDispute.dispute_id == dispute_id, StoredDispute.conversation_id == conversation_id,
            StoredDispute.rider_id == self._rider(rider_id),
        ).with_for_update().execution_options(populate_existing=True))
        if dispute is None:
            raise ConversationAccessError("Dispute not found for this rider and conversation")
        return dispute

    def create_dispute(self, rider_id: str, *, conversation_id: str, claim: DisputeRequest) -> StoredDispute:
        conversation = self._conversation(rider_id, conversation_id)
        claim = DisputeRequest.model_validate(claim.model_dump() if isinstance(claim, DisputeRequest) else claim)
        identifier = claim.dispute_id or str(uuid4())
        claim = claim.model_copy(update={"dispute_id": identifier})
        dispute = StoredDispute(
            dispute_id=identifier, conversation_id=conversation_id, rider_id=conversation.rider_id,
            dispute_type=claim.dispute_type.value, trip_id=claim.trip_id,
            dispute_date=claim.dispute_date, claimed_amount=claim.claimed_amount,
            claim=claim.model_dump(mode="json"),
        )
        self.session.add(dispute)
        self.session.flush()
        return dispute

    def update_dispute(
        self, rider_id: str, *, conversation_id: str, dispute_id: str,
        claim: DisputeRequest, expected_revision: int, reason: str = "claim clarified",
    ) -> StoredDispute:
        dispute = self._dispute(rider_id, conversation_id, dispute_id)
        claim = DisputeRequest.model_validate(claim.model_dump() if isinstance(claim, DisputeRequest) else claim)
        if claim.dispute_id is not None and claim.dispute_id != dispute_id:
            raise ValueError("A clarification cannot change dispute_id")
        if dispute.revision != expected_revision:
            raise ConversationConflictError("Dispute changed; reload before updating")
        history = dispute.revisions + [{"revision": dispute.revision, "reason": reason,
                                       "claim": dispute.claim, "status": dispute.status,
                                       "expected_amount": dispute.expected_amount,
                                       "actual_amount": dispute.actual_amount,
                                       "amount_owed": dispute.amount_owed,
                                       "explanation": dispute.explanation, "evidence": dispute.evidence}]
        claim = claim.model_copy(update={"dispute_id": dispute_id})
        values = dict(claim=claim.model_dump(mode="json"), revisions=history,
                      dispute_type=claim.dispute_type.value, trip_id=claim.trip_id,
                      dispute_date=claim.dispute_date, claimed_amount=claim.claimed_amount,
                      expected_amount=None, actual_amount=None, amount_owed=0,
                      status="needs_information", explanation="", evidence={}, is_active=True)
        return self._update_dispute(dispute, expected_revision, values)

    def record_dispute_result(
        self, rider_id: str, *, conversation_id: str, result: Dispute, expected_revision: int,
    ) -> StoredDispute:
        result = Dispute.model_validate(result.model_dump() if isinstance(result, Dispute) else result)
        dispute = self._dispute(rider_id, conversation_id, result.dispute_id)
        claim = DisputeRequest.model_validate(dispute.claim)
        if (result.rider_id != self._rider(rider_id) or result.dispute_type != claim.dispute_type
                or result.trip_id != claim.trip_id or result.claimed_amount != claim.claimed_amount
                or (claim.dispute_date is not None and result.dispute_date != claim.dispute_date)):
            raise ConversationAccessError("Investigation does not match this rider's current claim")
        values = {key: getattr(result, key) for key in (
            "dispute_date", "expected_amount", "actual_amount", "amount_owed", "explanation", "evidence")}
        values["status"] = result.status.value
        return self._update_dispute(dispute, expected_revision, values)

    def resolve_dispute(self, rider_id: str, *, conversation_id: str, dispute_id: str,
                        expected_revision: int) -> StoredDispute:
        dispute = self._dispute(rider_id, conversation_id, dispute_id)
        return self._update_dispute(dispute, expected_revision, {"is_active": False})

    def _update_dispute(self, dispute: StoredDispute, revision: int, values: dict) -> StoredDispute:
        result = self.session.execute(update(StoredDispute).where(
            StoredDispute.dispute_id == dispute.dispute_id, StoredDispute.revision == revision,
        ).values(**values, revision=StoredDispute.revision + 1, updated_at=func.now())
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise ConversationConflictError("Dispute changed; reload before updating")
        self.session.refresh(dispute)
        return dispute
