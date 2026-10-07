"""Pure deterministic payout calculator.

All financial calculations use Decimal with ROUND_HALF_UP (0.5 rounds up),
avoiding Python's default banker's rounding.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

from app.domain.policy import (
    BASE_FARE_RUPEES,
    BASE_DISTANCE_KM,
    RATE_PER_EXTRA_KM_RUPEES,
    DAILY_INCENTIVE_RUPEES,
    DAILY_INCENTIVE_THRESHOLD,
    RIDER_CANCELLATION_PENALTY_RUPEES,
    CUSTOMER_CANCELLATION_PENALTY_RUPEES,
    TripStatus,
    normalize_distance_km,
)


def calculate_base_and_distance_fare(distance_km: Union[Decimal, float, str, int]) -> Decimal:
    """Calculate trip fare before surge: ₹25 + ₹6 * max(distance_km - 2, 0).

    Returns exact Decimal value.
    """
    dist = normalize_distance_km(distance_km)
    extra_km = max(Decimal("0"), dist - BASE_DISTANCE_KM)
    return BASE_FARE_RUPEES + (RATE_PER_EXTRA_KM_RUPEES * extra_km)


def calculate_trip_fare(
    distance_km: Union[Decimal, float, str, int],
    surge_multiplier: Union[Decimal, float, str, int] = Decimal("1.0"),
) -> int:
    """Calculate payout for a completed trip in whole rupees.

    Formula:
      base_fare = 25 + 6 * max(distance_km - 2, 0)
      total_fare = base_fare * surge_multiplier
      rounded = round_half_up(total_fare)
    """
    base_dist_fare = calculate_base_and_distance_fare(distance_km)
    surge = Decimal(str(surge_multiplier))
    if not surge.is_finite() or surge <= 0:
        raise ValueError("Surge multiplier must be finite and positive")
    total_fare = base_dist_fare * surge
    # Round to nearest rupee, 0.5 rounds up
    return int(total_fare.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_cancellation_penalty(status: Union[str, TripStatus]) -> int:
    """Calculate cancellation penalty in rupees.

    Rider cancellations: -₹10 (represented as negative amount or 10 penalty)
    Customer cancellations: ₹0
    Completed trips: ₹0
    """
    s = TripStatus(status) if isinstance(status, str) else status
    if s == TripStatus.CANCELLED_BY_RIDER:
        return -RIDER_CANCELLATION_PENALTY_RUPEES
    return 0


def calculate_daily_incentive(completed_trips_count: int) -> int:
    """Calculate daily incentive based on completed trip count for the calendar day.

    Threshold: >= 12 completed trips yields ₹150 incentive.
    """
    if type(completed_trips_count) is not int or completed_trips_count < 0:
        raise ValueError("Completed trip count must be a nonnegative integer")
    if completed_trips_count >= DAILY_INCENTIVE_THRESHOLD:
        return DAILY_INCENTIVE_RUPEES
    return 0


def calculate_expected_trip_payout(
    status: Union[str, TripStatus],
    distance_km: Union[Decimal, float, str, int],
    surge_multiplier: Union[Decimal, float, str, int] = Decimal("1.0"),
) -> int:
    """Calculate expected payout for any trip given its status, distance, and surge."""
    s = TripStatus(status) if isinstance(status, str) else status
    if s == TripStatus.COMPLETED:
        return calculate_trip_fare(distance_km, surge_multiplier)
    elif s == TripStatus.CANCELLED_BY_RIDER:
        return -RIDER_CANCELLATION_PENALTY_RUPEES
    elif s == TripStatus.CANCELLED_BY_CUSTOMER:
        return CUSTOMER_CANCELLATION_PENALTY_RUPEES
    return 0
