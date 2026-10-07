"""Trace (audit log) service.

Phase 1: no trace steps are recorded yet, so every rider's trace is empty.
Persistence of steps is added together with the agent loop.
"""

from sqlalchemy.orm import Session

from app.schemas.trace import TraceStep


def get_trace(session: Session, rider_id: str) -> list[TraceStep]:
    """Return the ordered trace steps for ``rider_id``."""
    del session, rider_id  # Unused until trace persistence lands.
    return []
