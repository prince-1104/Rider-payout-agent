"""Trip service for querying and managing trips."""

from datetime import date, datetime, time, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Trip
from app.domain.policy import IST, normalize_rider_id


def get_trip_by_id(session: Session, trip_id: str) -> Optional[Trip]:
    """Fetch trip by ID."""
    stmt = select(Trip).where(Trip.trip_id == trip_id)
    return session.scalars(stmt).one_or_none()


def get_trips_for_rider_on_ist_date(
    session: Session, rider_id: str, target_date: date
) -> Sequence[Trip]:
    """Fetch all trips for a rider that started during the given IST calendar day."""
    norm_rider = normalize_rider_id(rider_id)

    # Convert IST day bounds to timestamps
    start_dt = datetime.combine(target_date, time.min, tzinfo=IST)
    end_dt = datetime.combine(target_date, time.max, tzinfo=IST)

    stmt = (
        select(Trip)
        .where(
            Trip.rider_id == norm_rider,
            Trip.started_at >= start_dt,
            Trip.started_at <= end_dt,
        )
        .order_by(Trip.started_at.asc())
    )
    return session.scalars(stmt).all()


def get_trips_for_rider(
    session: Session,
    rider_id: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Sequence[Trip]:
    """Fetch trips for a rider, optionally bounded by IST dates."""
    norm_rider = normalize_rider_id(rider_id)
    stmt = select(Trip).where(Trip.rider_id == norm_rider)

    if start_date is not None:
        start_dt = datetime.combine(start_date, time.min, tzinfo=IST)
        stmt = stmt.where(Trip.started_at >= start_dt)

    if end_date is not None:
        end_dt = datetime.combine(end_date, time.max, tzinfo=IST)
        stmt = stmt.where(Trip.started_at <= end_dt)

    stmt = stmt.order_by(Trip.started_at.asc())
    return session.scalars(stmt).all()
