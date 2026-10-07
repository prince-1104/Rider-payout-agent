"""Unit tests for deterministic domain logic and policy calculations.

Requirements covered:
1. base fare
2. distance calculation
3. surge
4. 0.5 rounding
5. daily incentive threshold
6. rider cancellation
7. customer cancellation
8. seven-day dispute boundary
9. invalid/unknown trip
10. payout reconciliation
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain.dispute_calculator import (
    reconcile_daily_incentive,
    reconcile_trip_payout,
)
from app.domain.payout_calculator import (
    calculate_base_and_distance_fare,
    calculate_cancellation_penalty,
    calculate_daily_incentive,
    calculate_expected_trip_payout,
    calculate_trip_fare,
)
from app.domain.policy import (
    BASE_FARE_RUPEES,
    DAILY_INCENTIVE_RUPEES,
    RIDER_CANCELLATION_PENALTY_RUPEES,
    TripStatus,
    is_dispute_within_window,
    normalize_distance_km,
    normalize_rider_id,
)


# ============================================================================
# 1. Base fare tests
# ============================================================================
def test_base_fare_for_trips_up_to_two_km() -> None:
    """Every completed trip pays ₹25 base for distance up to 2.0 km."""
    assert calculate_trip_fare(0.0) == 25
    assert calculate_trip_fare(0.8) == 25
    assert calculate_trip_fare(1.5) == 25
    assert calculate_trip_fare(2.0) == 25
    assert calculate_trip_fare(Decimal("2.0")) == 25


# ============================================================================
# 2. Distance calculation tests
# ============================================================================
def test_distance_calculation_above_two_km() -> None:
    """Distance beyond 2 km pays ₹6 for each km."""
    # 3.0 km -> 25 + 6 * (1.0) = 31
    assert calculate_trip_fare(3.0) == 31
    # 5.0 km -> 25 + 6 * (3.0) = 43
    assert calculate_trip_fare(5.0) == 43
    # 7.4 km -> 25 + 6 * (5.4) = 57.4 -> rounded to 57
    assert calculate_trip_fare(7.4) == 57
    # 10.0 km -> 25 + 6 * (8.0) = 73
    assert calculate_trip_fare(10.0) == 73


def test_distance_meter_normalization() -> None:
    """Distances >= 100 in trips.csv are stored in raw meters and must be divided by 1000."""
    assert normalize_distance_km(5000) == Decimal("5.0")
    assert normalize_distance_km(11200) == Decimal("11.2")
    # 5000m = 5.0 km -> 25 + 6*(3.0) = 43
    assert calculate_trip_fare(5000) == 43


# ============================================================================
# 3. Surge multiplier tests
# ============================================================================
def test_surge_multiplier_applied_to_entire_fare() -> None:
    """Surge applies to base + distance fare combined."""
    # 6.0 km at 1.5x surge:
    # Base fare = 25 + 6 * 4 = 49
    # Total with surge = 49 * 1.5 = 73.5 -> 74
    assert calculate_trip_fare(6.0, 1.5) == 74

    # 10.0 km at 1.2x surge:
    # Base fare = 25 + 6 * 8 = 73
    # Total with surge = 73 * 1.2 = 87.6 -> 88
    assert calculate_trip_fare(10.0, 1.2) == 88

    # 9.0 km at 1.5x surge:
    # Base fare = 25 + 6 * 7 = 67
    # Total with surge = 67 * 1.5 = 100.5 -> 101
    assert calculate_trip_fare(9.0, 1.5) == 101


# ============================================================================
# 4. 0.5 Rounding tests (ROUND_HALF_UP vs Banker's Rounding)
# ============================================================================
def test_round_half_up_differs_from_bankers_rounding() -> None:
    """0.5 must round UP, even when the integer part is even.

    Python's built-in round(64.5) rounds to 64 (banker's rounding).
    Policy requires 64.5 to round up to 65!
    """
    # 5.0 km at 1.5x surge:
    # Base fare = 25 + 6 * 3 = 43
    # 43 * 1.5 = 64.5
    # Python round(64.5) is 64!
    assert round(64.5) == 64
    # Our policy calculator must produce 65:
    assert calculate_trip_fare(5.0, 1.5) == 65

    # 6.0 km at 1.5x surge: 49 * 1.5 = 73.5 -> 74
    assert calculate_trip_fare(6.0, 1.5) == 74

    # 9.0 km at 1.5x surge: 67 * 1.5 = 100.5 -> 101
    assert calculate_trip_fare(9.0, 1.5) == 101


# ============================================================================
# 5. Daily incentive threshold tests
# ============================================================================
def test_daily_incentive_threshold() -> None:
    """₹150 incentive awarded ONLY when completed trips >= 12."""
    assert calculate_daily_incentive(0) == 0
    assert calculate_daily_incentive(10) == 0
    assert calculate_daily_incentive(11) == 0
    assert calculate_daily_incentive(12) == 150
    assert calculate_daily_incentive(13) == 150
    assert calculate_daily_incentive(20) == 150


# ============================================================================
# 6. Rider cancellation penalty tests
# ============================================================================
def test_rider_cancellation_penalty() -> None:
    """₹10 penalty for every trip the rider cancels."""
    assert calculate_cancellation_penalty(TripStatus.CANCELLED_BY_RIDER) == -10
    assert calculate_cancellation_penalty("cancelled_by_rider") == -10
    assert (
        calculate_expected_trip_payout(TripStatus.CANCELLED_BY_RIDER, distance_km=5.0)
        == -10
    )


def test_rider_cancellation_duplicate_penalty_reconciliation() -> None:
    """When a rider cancellation penalty was deducted twice (-20), ₹10 refund is owed."""
    reconciled = reconcile_trip_payout(
        trip_id="T637155",
        rider_id="R014",
        status=TripStatus.CANCELLED_BY_RIDER,
        distance_km=3.0,
        surge_multiplier=1.0,
        trip_payout_lines=[-10, -10],  # Deducted twice! Total -20
    )
    assert reconciled.expected_amount == -10
    assert reconciled.actual_paid_amount == -20
    assert reconciled.amount_owed == 10  # Refund ₹10
    assert reconciled.discrepancy_type == "duplicate_penalty"


# ============================================================================
# 7. Customer cancellation tests
# ============================================================================
def test_customer_cancellation_earns_zero() -> None:
    """Customer cancellations earn ₹0 and incur no penalty."""
    assert calculate_cancellation_penalty(TripStatus.CANCELLED_BY_CUSTOMER) == 0
    assert calculate_cancellation_penalty("cancelled_by_customer") == 0
    assert (
        calculate_expected_trip_payout(TripStatus.CANCELLED_BY_CUSTOMER, distance_km=9.1)
        == 0
    )

    reconciled = reconcile_trip_payout(
        trip_id="T203704",
        rider_id="R035",
        status=TripStatus.CANCELLED_BY_CUSTOMER,
        distance_km=9.1,
        surge_multiplier=1.0,
        trip_payout_lines=[],
    )
    assert reconciled.expected_amount == 0
    assert reconciled.actual_paid_amount == 0
    assert reconciled.amount_owed == 0


# ============================================================================
# 8. Seven-day dispute boundary tests
# ============================================================================
def test_seven_day_dispute_boundary() -> None:
    """Disputes are accepted only for the last 7 calendar days."""
    today = date(2026, 9, 23)

    # Within 7 days
    assert is_dispute_within_window(today, reference_date=today) is True
    assert is_dispute_within_window(today - timedelta(days=1), reference_date=today) is True
    assert is_dispute_within_window(today - timedelta(days=6), reference_date=today) is True
    assert is_dispute_within_window(today - timedelta(days=7), reference_date=today) is True

    # Day 8 or older is expired
    assert is_dispute_within_window(today - timedelta(days=8), reference_date=today) is False
    assert is_dispute_within_window(today - timedelta(days=10), reference_date=today) is False

    # Future dates are invalid
    assert is_dispute_within_window(today + timedelta(days=1), reference_date=today) is False


# ============================================================================
# 9. Invalid/Unknown trip handling
# ============================================================================
def test_unknown_status_handling() -> None:
    """Unknown statuses default to 0 expected payout safely."""
    reconciled = reconcile_trip_payout(
        trip_id="T_UNKNOWN",
        rider_id="R001",
        status="invalid_status",
        distance_km=5.0,
        surge_multiplier=1.0,
        trip_payout_lines=[],
    )
    assert reconciled.expected_amount == 0
    assert reconciled.amount_owed == 0


def test_rider_id_normalization() -> None:
    """Handles inconsistent casing and padding: r19 -> R019, R7 -> R007."""
    assert normalize_rider_id("r19") == "R019"
    assert normalize_rider_id("R7") == "R007"
    assert normalize_rider_id("R003") == "R003"
    assert normalize_rider_id("  r25  ") == "R025"


# ============================================================================
# 10. Payout reconciliation tests
# ============================================================================
def test_reconcile_trip_surge_missing() -> None:
    """Reconciles trip T926334 where 1.5x surge was omitted in payout."""
    # Distance 6.0 km, surge 1.5x. Expected 74, paid 49.
    reconciled = reconcile_trip_payout(
        trip_id="T926334",
        rider_id="R003",
        status=TripStatus.COMPLETED,
        distance_km=6.0,
        surge_multiplier=1.5,
        trip_payout_lines=[49],
    )
    assert reconciled.expected_amount == 74
    assert reconciled.actual_paid_amount == 49
    assert reconciled.amount_owed == 25
    assert reconciled.discrepancy_type == "surge_missing"


def test_reconcile_trip_unpaid() -> None:
    """Reconciles trip completed but missing entirely from payout lines."""
    # Distance 13.0 km, surge 1.0x. Fare = 25 + 6*11 = 91.
    reconciled = reconcile_trip_payout(
        trip_id="T481678",
        rider_id="R016",
        status=TripStatus.COMPLETED,
        distance_km=13.0,
        surge_multiplier=1.0,
        trip_payout_lines=[],
    )
    assert reconciled.expected_amount == 91
    assert reconciled.actual_paid_amount == 0
    assert reconciled.amount_owed == 91
    assert reconciled.discrepancy_type == "unpaid"


def test_reconcile_trip_fully_paid() -> None:
    """Fully paid trip has 0 amount owed."""
    reconciled = reconcile_trip_payout(
        trip_id="T252921",
        rider_id="R008",
        status=TripStatus.COMPLETED,
        distance_km=5.5,
        surge_multiplier=1.0,
        trip_payout_lines=[46],
    )
    assert reconciled.expected_amount == 46
    assert reconciled.actual_paid_amount == 46
    assert reconciled.amount_owed == 0
    assert reconciled.discrepancy_type is None


def test_reconcile_daily_incentive_missing_and_earned() -> None:
    """13 trips completed on 21 Sep, 0 incentive paid -> ₹150 owed."""
    audit = reconcile_daily_incentive(
        rider_id="R005",
        payout_date=date(2026, 9, 21),
        completed_trips_count=13,
        incentive_payout_lines=[],
    )
    assert audit.incentive_earned is True
    assert audit.expected_amount == 150
    assert audit.actual_paid_amount == 0
    assert audit.amount_owed == 150


def test_reconcile_daily_incentive_not_earned() -> None:
    """10 trips completed -> incentive not earned, 0 owed."""
    audit = reconcile_daily_incentive(
        rider_id="R011",
        payout_date=date(2026, 9, 19),
        completed_trips_count=10,
        incentive_payout_lines=[],
    )
    assert audit.incentive_earned is False
    assert audit.expected_amount == 0
    assert audit.amount_owed == 0
