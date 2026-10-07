"""Data-access layer. Repositories never commit; the calling service owns the transaction."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import InboundMessage


class InboundMessageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_message_id(self, message_id: str) -> InboundMessage | None:
        stmt = select(InboundMessage).where(InboundMessage.message_id == message_id)
        return self._session.scalars(stmt).one_or_none()

    def add(
        self,
        *,
        message_id: str,
        rider_id: str,
        text: str,
        received_at: datetime,
    ) -> InboundMessage:
        """Stage and flush a new message. Raises ``IntegrityError`` on a duplicate ``message_id``."""
        message = InboundMessage(
            message_id=message_id,
            rider_id=rider_id,
            text=text,
            received_at=received_at,
        )
        self._session.add(message)
        self._session.flush()
        return message

    def set_reply(self, message: InboundMessage, reply: str) -> None:
        message.reply = reply
        self._session.flush()
