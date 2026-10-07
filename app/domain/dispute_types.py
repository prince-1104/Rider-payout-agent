"""Supported claims and deterministic investigation outcomes."""

from enum import Enum


class DisputeType(str, Enum):
    MISSING_TRIP_PAYOUT = "missing_trip_payout"
    MISSING_SURGE = "missing_surge"
    INCORRECT_TRIP_PAYOUT = "incorrect_trip_payout"
    MISSING_DAILY_INCENTIVE = "missing_daily_incentive"
    INCORRECT_DAILY_INCENTIVE = "incorrect_daily_incentive"
    DUPLICATE_CANCELLATION_PENALTY = "duplicate_cancellation_penalty"
    INCORRECT_CANCELLATION_PENALTY = "incorrect_cancellation_penalty"
    GENERAL_PAYOUT_DISPUTE = "general_payout_dispute"
    UNKNOWN = "unknown"


class DisputeStatus(str, Enum):
    VALID = "valid"
    REJECTED = "rejected"
    NEEDS_INFORMATION = "needs_information"
    NEEDS_REVIEW = "needs_review"


INCENTIVE_TYPES = frozenset({DisputeType.MISSING_DAILY_INCENTIVE, DisputeType.INCORRECT_DAILY_INCENTIVE})
PENALTY_TYPES = frozenset({DisputeType.DUPLICATE_CANCELLATION_PENALTY, DisputeType.INCORRECT_CANCELLATION_PENALTY})
