"""QuickDrop payout policy definitions and constants.

Based directly on docs/policy.md:
- Every completed trip pays ₹25 base + ₹6 for each km after the first 2 km.
- If the trip had a surge multiplier, it applies to the whole trip fare (base + distance).
- Round each trip to the nearest rupee, 0.5 rounds up.
- Daily incentive of ₹150 for 12 or more trips in a day.
- ₹10 penalty for every trip the rider cancels. Customer cancellations earn nothing.
- "Day" means the calendar day in IST (UTC+05:30).
- We only look at disputes for the last 7 days.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Union

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Policy Financial Constants (Decimal for exact arithmetic)
BASE_FARE_RUPEES = Decimal("25")
BASE_DISTANCE_KM = Decimal("2.0")
RATE_PER_EXTRA_KM_RUPEES = Decimal("6")

DAILY_INCENTIVE_RUPEES = 150
DAILY_INCENTIVE_THRESHOLD = 12

RIDER_CANCELLATION_PENALTY_RUPEES = 10
CUSTOMER_CANCELLATION_PENALTY_RUPEES = 0

DISPUTE_VALID_WINDOW_DAYS = 7
AUTO_PAYMENT_LIMIT_RUPEES = 200


class TripStatus(str, Enum):
    COMPLETED = "completed"
    CANCELLED_BY_RIDER = "cancelled_by_rider"
    CANCELLED_BY_CUSTOMER = "cancelled_by_customer"


class PayoutLineType(str, Enum):
    TRIP = "trip"
    DAILY_INCENTIVE = "daily_incentive"
    CANCELLATION_PENALTY = "cancellation_penalty"


def normalize_rider_id(raw_rider_id: str) -> str:
    """Normalize rider IDs such as 'r19' -> 'R019' or 'R7' -> 'R007'."""
    trimmed = raw_rider_id.strip()
    if trimmed.lower().startswith("r"):
        num_part = trimmed[1:]
        if num_part.isdigit():
            return f"R{int(num_part):03d}"
    return trimmed.upper()


def normalize_distance_km(distance: Union[Decimal, float, str, int]) -> Decimal:
    """Normalize distance value.

    In trips.csv, distances exceeding 100 represent raw meters (e.g., 5000 = 5.0 km, 11200 = 11.2 km).
    Values below 100 represent kilometers directly.
    """
    d = Decimal(str(distance))
    if not d.is_finite() or d < 0:
        raise ValueError("Distance must be finite and nonnegative")
    if d >= Decimal("100"):
        return d / Decimal("1000")
    return d


def to_ist_date(dt: datetime) -> date:
    """Convert any datetime (aware or naive UTC) to its calendar date in IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).date()


def is_dispute_within_window(
    target_date: date,
    reference_date: Union[date, datetime, None] = None,
    max_days: int = DISPUTE_VALID_WINDOW_DAYS,
) -> bool:
    """Check if a dispute date falls within the policy window (last 7 days).

    `reference_date` is the date on which the dispute is received (default: current IST date).
    """
    if reference_date is None:
        ref_d = datetime.now(timezone.utc).astimezone(IST).date()
    elif isinstance(reference_date, datetime):
        ref_d = to_ist_date(reference_date)
    else:
        ref_d = reference_date

    diff = (ref_d - target_date).days
    return 0 <= diff <= max_days
