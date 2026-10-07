"""Dispute and payout reconciliation domain models and calculators."""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from app.domain.payout_calculator import (
    calculate_trip_fare,
    calculate_daily_incentive,
)
from app.domain.policy import (
    TripStatus,
    PayoutLineType,
    RIDER_CANCELLATION_PENALTY_RUPEES,
    is_dispute_within_window,
)


@dataclass(frozen=True)
class TripReconciliation:
    """Detailed audit for a single trip."""

    trip_id: str
    rider_id: str
    status: str
    distance_km: float
    surge_multiplier: float
    expected_amount: int
    actual_paid_amount: int
    amount_owed: int  # max(0, expected - actual_paid)
    discrepancy_type: Optional[str] = None  # 'surge_missing', 'unpaid', 'underpaid', 'duplicate_penalty', None
    note: str = ""


@dataclass(frozen=True)
class DailyIncentiveReconciliation:
    """Detailed audit for a rider's daily incentive."""

    rider_id: str
    payout_date: date
    completed_trips_count: int
    incentive_earned: bool
    expected_amount: int  # 150 or 0
    actual_paid_amount: int  # 150 or 0
    amount_owed: int  # max(0, expected - actual_paid)
    note: str = ""


@dataclass
class DailyDisputeAudit:
    """Complete audit of a rider's trips and payouts for an IST calendar day."""

    rider_id: str
    date: date
    is_within_policy_window: bool
    completed_trips_count: int
    cancelled_by_rider_count: int
    cancelled_by_customer_count: int
    trip_discrepancies: list[TripReconciliation] = field(default_factory=list)
    penalty_discrepancies: list[TripReconciliation] = field(default_factory=list)
    incentive_audit: Optional[DailyIncentiveReconciliation] = None
    total_expected: int = 0
    total_paid: int = 0
    total_owed: int = 0


def reconcile_trip_payout(
    trip_id: str,
    rider_id: str,
    status: str,
    distance_km: float,
    surge_multiplier: float,
    trip_payout_lines: Sequence[int],  # Amounts of 'trip' or 'cancellation_penalty' lines
) -> TripReconciliation:
    """Reconcile expected vs actual payout for a single trip."""
    actual_paid = sum(trip_payout_lines)

    if status == TripStatus.COMPLETED:
        expected = calculate_trip_fare(distance_km, surge_multiplier)
        owed = max(0, expected - actual_paid)
        discrepancy_type = None
        note = ""

        if owed > 0:
            fare_no_surge = calculate_trip_fare(distance_km, 1.0)
            if actual_paid == 0:
                discrepancy_type = "unpaid"
                note = f"Trip {trip_id} was completed but not paid. Expected ₹{expected}."
            elif float(surge_multiplier) > 1.0 and actual_paid == fare_no_surge:
                discrepancy_type = "surge_missing"
                note = f"Trip {trip_id} surge {surge_multiplier}x missing. Expected ₹{expected}, paid ₹{actual_paid}. Difference ₹{owed}."
            else:
                discrepancy_type = "underpaid"
                note = f"Trip {trip_id} underpaid by ₹{owed}. Expected ₹{expected}, paid ₹{actual_paid}."

        return TripReconciliation(
            trip_id=trip_id,
            rider_id=rider_id,
            status=status,
            distance_km=float(distance_km),
            surge_multiplier=float(surge_multiplier),
            expected_amount=expected,
            actual_paid_amount=actual_paid,
            amount_owed=owed,
            discrepancy_type=discrepancy_type,
            note=note,
        )

    elif status == TripStatus.CANCELLED_BY_RIDER:
        # Expected is -10 (penalty)
        expected = -RIDER_CANCELLATION_PENALTY_RUPEES
        owed = 0
        discrepancy_type = None
        note = ""

        # Check duplicate penalty deductions (e.g. -20 instead of -10)
        if actual_paid < -RIDER_CANCELLATION_PENALTY_RUPEES:
            # Over-deducted, owed refund
            refund = abs(actual_paid) - RIDER_CANCELLATION_PENALTY_RUPEES
            owed = refund
            discrepancy_type = "duplicate_penalty"
            note = f"Duplicate cancellation penalty deducted for trip {trip_id}. Refund owed: ₹{refund}."

        return TripReconciliation(
            trip_id=trip_id,
            rider_id=rider_id,
            status=status,
            distance_km=float(distance_km),
            surge_multiplier=float(surge_multiplier),
            expected_amount=expected,
            actual_paid_amount=actual_paid,
            amount_owed=owed,
            discrepancy_type=discrepancy_type,
            note=note,
        )

    elif status == TripStatus.CANCELLED_BY_CUSTOMER:
        # Customer cancellations earn 0 and incur 0 penalty
        expected = 0
        owed = max(0, expected - actual_paid)
        return TripReconciliation(
            trip_id=trip_id,
            rider_id=rider_id,
            status=status,
            distance_km=float(distance_km),
            surge_multiplier=float(surge_multiplier),
            expected_amount=0,
            actual_paid_amount=actual_paid,
            amount_owed=owed,
            discrepancy_type=None,
            note="",
        )

    return TripReconciliation(
        trip_id=trip_id,
        rider_id=rider_id,
        status=status,
        distance_km=float(distance_km),
        surge_multiplier=float(surge_multiplier),
        expected_amount=0,
        actual_paid_amount=actual_paid,
        amount_owed=0,
    )


def reconcile_daily_incentive(
    rider_id: str,
    payout_date: date,
    completed_trips_count: int,
    incentive_payout_lines: Sequence[int],
) -> DailyIncentiveReconciliation:
    """Audit daily incentive for a rider on a given IST calendar day."""
    earned = completed_trips_count >= 12
    expected = calculate_daily_incentive(completed_trips_count)
    actual_paid = sum(incentive_payout_lines)
    owed = max(0, expected - actual_paid)

    note = ""
    if earned and owed > 0:
        note = f"Rider completed {completed_trips_count} trips on {payout_date}. ₹150 incentive was not paid."
    elif not earned and actual_paid == 0:
        note = f"Rider completed {completed_trips_count} trips on {payout_date} (threshold is 12). Incentive not earned."

    return DailyIncentiveReconciliation(
        rider_id=rider_id,
        payout_date=payout_date,
        completed_trips_count=completed_trips_count,
        incentive_earned=earned,
        expected_amount=expected,
        actual_paid_amount=actual_paid,
        amount_owed=owed,
        note=note,
    )
