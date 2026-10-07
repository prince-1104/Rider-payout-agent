"""Read-only database adapter for deterministic dispute investigation."""

from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import PayoutLine, Trip
from app.domain.dispute import Dispute, DisputeRequest
from app.domain.policy import normalize_rider_id
from app.domain.reconciliation import PayoutRecord, TripRecord, evaluate_dispute


class DisputeService:
    """Investigate using authenticated identity, never an identity from rider text.

    Does not commit, modify data, approve claims or call PaySwift. Snapshot all
    rider trips and their ledger entries so late postings are not mistaken for
    missing payments. IST grouping happens in the pure engine, consistently on
    PostgreSQL and SQLite (which drops timezone offsets).
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def evaluate(
        self, authenticated_rider_id: str, request: DisputeRequest,
        *, reference_date: date | datetime,
    ) -> Dispute:
        if not isinstance(authenticated_rider_id, str) or not authenticated_rider_id.strip():
            raise ValueError("authenticated_rider_id is required")
        rider = normalize_rider_id(authenticated_rider_id)
        trips = self.session.scalars(select(Trip).where(Trip.rider_id == rider)).all()
        own_trip_ids = select(Trip.trip_id).where(Trip.rider_id == rider)
        lines = self.session.scalars(select(PayoutLine).where(or_(
            PayoutLine.rider_id == rider, PayoutLine.trip_id.in_(own_trip_ids),
        ))).all()
        return evaluate_dispute(
            request, authenticated_rider_id=rider, reference_date=reference_date,
            trips=[TripRecord(t.trip_id, t.rider_id, t.started_at, t.status,
                              t.distance_km, t.surge_multiplier) for t in trips],
            payout_lines=[PayoutRecord(p.line_id, p.rider_id, p.payout_date,
                                       p.line_type, p.trip_id, p.amount) for p in lines],
        )
