"""Persistent rider-scoped audit trail for tool calls and payment decisions."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from sqlalchemy.orm import Session

from app.agent.security import redact_secrets
from app.schemas.trace import TraceStep
from app.db.models import TraceEvent


class TraceRecorder:
    """Own short transactions so a failed invocation remains visible after rollback."""

    def __init__(self, session_factory, rider_id: str):
        self.session_factory = session_factory
        self.rider_id = rider_id

    def start(self, name: str, inputs, *, kind="tool_call") -> int:
        with self.session_factory() as session:
            event = TraceEvent(invocation_id=str(uuid4()), rider_id=self.rider_id,
                               at=datetime.now(timezone.utc), type=kind, name=name,
                               input=jsonable_encoder(redact_secrets(inputs)), output=None)
            session.add(event)
            session.commit()
            return event.id

    def finish(self, identifier: int, output=None, error: Exception | None = None):
        with self.session_factory() as session:
            event = session.get(TraceEvent, identifier)
            if event is None or event.rider_id != self.rider_id:
                raise ValueError("Trace invocation does not belong to this rider")
            if error is None:
                event.output = jsonable_encoder(redact_secrets(output))
            else:
                # No provider bodies, credentials or foreign rider data in errors.
                payload = {"error": type(error).__name__, "message": "Invocation failed; inspect validated inputs and policy"}
                event.output = payload
                session.add(TraceEvent(invocation_id=event.invocation_id, rider_id=self.rider_id,
                                       at=datetime.now(timezone.utc), type="error", name=event.name,
                                       input=event.input, output=payload))
            session.commit()


def get_trace(session: Session, rider_id: str) -> list[TraceStep]:
    """Return the ordered trace steps for ``rider_id``."""
    events = session.scalars(select(TraceEvent).where(TraceEvent.rider_id == rider_id)
                             .order_by(TraceEvent.at, TraceEvent.id)).all()
    return [TraceStep(at=e.at.replace(tzinfo=timezone.utc) if e.at.tzinfo is None else e.at,
                      type=e.type, name=e.name, input=e.input, output=e.output) for e in events]
