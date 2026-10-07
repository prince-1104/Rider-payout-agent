"""Exactly-once handling of vendor message deliveries, keyed by ``message_id`` (wamid).

The insert and the stored reply are committed in one transaction. On PostgreSQL a
concurrent delivery of the same wamid blocks on the unique index until the first
request commits, then fails with ``IntegrityError`` and replays the stored reply.
"""

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import InboundMessage
from app.db.repositories import InboundMessageRepository
from app.schemas.messages import InboundMessageIn


@dataclass(frozen=True, slots=True)
class Registration:
    message: InboundMessage
    is_duplicate: bool


def register_inbound_message(session: Session, payload: InboundMessageIn) -> Registration:
    """Insert the message, or return the already-stored one if this wamid was seen before.

    Does not commit; call :func:`record_reply` to finish the transaction.
    """
    repo = InboundMessageRepository(session)

    existing = repo.get_by_message_id(payload.message_id)
    if existing is not None:
        return Registration(message=existing, is_duplicate=True)

    try:
        with session.begin_nested():
            message = repo.add(
                message_id=payload.message_id,
                rider_id=payload.rider_id,
                text=payload.text,
                received_at=payload.received_at,
            )
    except IntegrityError:
        # Lost a race with a concurrent delivery of the same wamid.
        existing = repo.get_by_message_id(payload.message_id)
        if existing is None:
            raise
        return Registration(message=existing, is_duplicate=True)

    return Registration(message=message, is_duplicate=False)


def record_reply(session: Session, message: InboundMessage, reply: str) -> None:
    """Persist the reply for a newly registered message and commit."""
    InboundMessageRepository(session).set_reply(message, reply)
    session.commit()
