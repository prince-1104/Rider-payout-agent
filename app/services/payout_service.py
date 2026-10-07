"""Payout service for auditing and reconciling payout lines."""

from datetime import date, datetime
from typing import Optional, Sequence, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PayoutLine, Trip
from app.domain.dispute_calculator import (
    DailyDisputeAudit,
    DailyIncentiveReconciliation,
    TripReconciliation,
    reconcile_daily_incentive,
    reconcile_trip_payout,
)
from app.domain.policy import (
    PayoutLineType,
    TripStatus,
    is_dispute_within_window,
    normalize_rider_id,
)
from app.services.trip_service import get_trip_by_id, get_trips_for_rider_on_ist_date


def get_payout_lines_for_trip(session: Session, trip_id: str) -> Sequence[PayoutLine]:
    """Fetch all payout lines referencing a specific trip_id."""
    stmt = select(PayoutLine).where(PayoutLine.trip_id == trip_id)
    return session.scalars(stmt).all()


def get_payout_lines_for_rider_on_date(
    session: Session, rider_id: str, payout_date: date
) -> Sequence[PayoutLine]:
    """Fetch all payout lines for a rider on a given payout_date."""
    norm_rider = normalize_rider_id(rider_id)
    stmt = (
        select(PayoutLine)
        .where(
            PayoutLine.rider_id == norm_rider,
            PayoutLine.payout_date == payout_date,
        )
        .order_by(PayoutLine.line_id.asc())
    )
    return session.scalars(stmt).all()


def audit_trip(session: Session, trip_id: str) -> Optional[TripReconciliation]:
    """Audit and reconcile a single trip against its payout lines."""
    trip = get_trip_by_id(session, trip_id)
    if trip is None:
        return None

    lines = get_payout_lines_for_trip(session, trip_id)
    amounts = [line.amount for line in lines]

    return reconcile_trip_payout(
        trip_id=trip.trip_id,
        rider_id=trip.rider_id,
        status=trip.status,
        distance_km=float(trip.distance_km),
        surge_multiplier=float(trip.surge_multiplier),
        trip_payout_lines=amounts,
    )


def audit_rider_day(
    session: Session,
    rider_id: str,
    target_date: date,
    reference_date: Union[date, datetime, None] = None,
) -> DailyDisputeAudit:
    """Perform a full daily audit of trips and payouts for a rider on a calendar day."""
    norm_rider = normalize_rider_id(rider_id)
    trips = get_trips_for_rider_on_ist_date(session, norm_rider, target_date)
    payout_lines = get_payout_lines_for_rider_on_date(session, norm_rider, target_date)

    within_window = is_dispute_within_window(target_date, reference_date)

    completed_trips = [t for t in trips if t.status == TripStatus.COMPLETED]
    rider_cancels = [t for t in trips if t.status == TripStatus.CANCELLED_BY_RIDER]
    cust_cancels = [t for t in trips if t.status == TripStatus.CANCELLED_BY_CUSTOMER]

    # Map payout lines by trip_id
    lines_by_trip: dict[str, list[int]] = {}
    incentive_lines: list[int] = []

    for line in payout_lines:
        if line.line_type == PayoutLineType.DAILY_INCENTIVE:
            incentive_lines.append(line.amount)
        elif line.trip_id:
            lines_by_trip.setdefault(line.trip_id, []).append(line.amount)

    trip_discrepancies: list[TripReconciliation] = []
    penalty_discrepancies: list[TripReconciliation] = []

    total_expected = 0
    total_paid = sum(l.amount for l in payout_lines)

    for trip in trips:
        trip_amounts = lines_by_trip.get(trip.trip_id, [])
        reconciled = reconcile_trip_payout(
            trip_id=trip.trip_id,
            rider_id=trip.rider_id,
            status=trip.status,
            distance_km=float(trip.distance_km),
            surge_multiplier=float(trip.surge_multiplier),
            trip_payout_lines=trip_amounts,
        )
        total_expected += reconciled.expected_amount

        if reconciled.status == TripStatus.COMPLETED and reconciled.amount_owed > 0:
            trip_discrepancies.append(reconciled)
        elif reconciled.status == TripStatus.CANCELLED_BY_RIDER and reconciled.amount_owed > 0:
            penalty_discrepancies.append(reconciled)

    # Reconcile incentive
    incentive_audit = reconcile_daily_incentive(
        rider_id=norm_rider,
        payout_date=target_date,
        completed_trips_count=len(completed_trips),
        incentive_payout_lines=incentive_lines,
    )
    total_expected += incentive_audit.expected_amount

    # Total owed = sum of trip underpayments + duplicate penalty refunds + missing incentive
    total_owed = (
        sum(d.amount_owed for d in trip_discrepancies)
        + sum(p.amount_owed for p in penalty_discrepancies)
        + incentive_audit.amount_owed
    )

    return DailyDisputeAudit(
        rider_id=norm_rider,
        date=target_date,
        is_within_policy_window=within_window,
        completed_trips_count=len(completed_trips),
        cancelled_by_rider_count=len(rider_cancels),
        cancelled_by_customer_count=len(cust_cancels),
        trip_discrepancies=trip_discrepancies,
        penalty_discrepancies=penalty_discrepancies,
        incentive_audit=incentive_audit,
        total_expected=total_expected,
        total_paid=total_paid,
        total_owed=total_owed,
    )
