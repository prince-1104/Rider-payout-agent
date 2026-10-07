"""Exactly-once handling of vendor message deliveries, keyed by ``message_id`` (wamid).

Registration uses a unique index and race recovery. The webhook commits inbound
data before the workflow, claims durable processing leases, then caches the final
reply separately. Financial reservations are durable before provider side effects.
"""

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import InboundMessage
from app.db.database import begin_savepoint
from app.db.repositories import InboundMessageRepository
from app.schemas.messages import InboundMessageIn


@dataclass(frozen=True, slots=True)
class Registration:
    message: InboundMessage
    is_duplicate: bool


class MessageIdentityConflict(ValueError):
    """A vendor ID was reused under a different authoritative request identity."""


def register_inbound_message(session: Session, payload: InboundMessageIn) -> Registration:
    """Insert the message, or return the already-stored one if this wamid was seen before.

    Does not commit; the application owns transaction boundaries.
    """
    repo = InboundMessageRepository(session)

    existing = repo.get_by_message_id(payload.message_id)
    if existing is not None:
        if existing.rider_id != payload.rider_id:
            raise MessageIdentityConflict("Message ID belongs to another rider")
        return Registration(message=existing, is_duplicate=True)

    try:
        with begin_savepoint(session):
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
        if existing.rider_id != payload.rider_id:
            raise MessageIdentityConflict("Message ID belongs to another rider")
        return Registration(message=existing, is_duplicate=True)

    return Registration(message=message, is_duplicate=False)


def record_reply(session: Session, message: InboundMessage, reply: str) -> None:
    """Persist the reply for a newly registered message and commit."""
    InboundMessageRepository(session).set_reply(message, reply)
    session.commit()
