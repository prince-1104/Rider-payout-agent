"""Pure financial reconciliation over trusted dispatch and ledger snapshots."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, TypeVar
from uuid import NAMESPACE_URL, uuid5

from app.domain.dispute import Dispute, DisputeRequest
from app.domain.dispute_types import DisputeStatus, DisputeType, INCENTIVE_TYPES, PENALTY_TYPES
from app.domain.payout_calculator import (
    calculate_cancellation_penalty, calculate_daily_incentive,
    calculate_expected_trip_payout, calculate_trip_fare,
)
from app.domain.policy import (
    DAILY_INCENTIVE_THRESHOLD, DISPUTE_VALID_WINDOW_DAYS, PayoutLineType, TripStatus,
    is_dispute_within_window, normalize_distance_km, normalize_rider_id, to_ist_date,
)


@dataclass(frozen=True)
class TripRecord:
    trip_id: str
    rider_id: str
    started_at: datetime
    status: str
    distance_km: Decimal
    surge_multiplier: Decimal


@dataclass(frozen=True)
class PayoutRecord:
    line_id: str
    rider_id: str
    payout_date: date
    line_type: str
    trip_id: str | None
    amount: int


class EvidenceError(ValueError):
    """Conflicting or malformed authoritative records require human review."""


Record = TypeVar("Record", TripRecord, PayoutRecord)


def _unique(records: Iterable[Record], key: str) -> list[Record]:
    by_id: dict[str, Record] = {}
    for record in records:
        identifier = getattr(record, key)
        if identifier in by_id and by_id[identifier] != record:
            raise EvidenceError("Conflicting records share an identifier; reconcile the source data.")
        by_id[identifier] = record
    return [by_id[k] for k in sorted(by_id)]


def evaluate_dispute(
    request: DisputeRequest,
    *,
    authenticated_rider_id: str,
    trips: Iterable[TripRecord],
    payout_lines: Iterable[PayoutRecord],
    reference_date: date | datetime,
) -> Dispute:
    """Evaluate a claim from supplied records without I/O, LLMs or side effects.

    The trusted reference date is explicit to make historical evaluation
    reproducible. Trip dates come from dispatch in IST, never from rider text.
    Seven days means age 0 through 7 inclusive, matching the existing policy.
    General daily claims reconcile the *net* day shortfall, including overpayments.
    Trip-linked ledger entries are reconciled across posting dates.
    """
    if not isinstance(authenticated_rider_id, str) or not authenticated_rider_id.strip():
        raise ValueError("authenticated_rider_id is required")
    rider = normalize_rider_id(authenticated_rider_id)
    target_date = request.dispute_date
    evidence: dict = {"policy_window_days": DISPUTE_VALID_WINDOW_DAYS, "reference_date": (
        to_ist_date(reference_date) if isinstance(reference_date, datetime) else reference_date
    ).isoformat()}

    def finish(status: DisputeStatus, reason: str, expected: int | None = None,
               actual: int | None = None, owed: int = 0) -> Dispute:
        # Stable investigation identity, not a payment idempotency mechanism.
        identity = f"quickdrop:{rider}:{request.dispute_type.value}:{request.trip_id}:{target_date}"
        return Dispute(
            dispute_id=request.dispute_id or str(uuid5(NAMESPACE_URL, identity)),
            rider_id=rider, dispute_type=request.dispute_type, trip_id=request.trip_id,
            dispute_date=target_date, claimed_amount=request.claimed_amount,
            expected_amount=expected, actual_amount=actual, amount_owed=owed,
            status=status, explanation=reason, evidence=evidence,
        )

    if request.dispute_type == DisputeType.UNKNOWN:
        return finish(DisputeStatus.NEEDS_REVIEW, "Unsupported dispute; ops must review the claim.")
    if request.dispute_type in INCENTIVE_TYPES and request.trip_id:
        return finish(DisputeStatus.NEEDS_INFORMATION, "Daily incentives require a calendar date, not a trip ID.")

    try:
        own_trips = _unique((t for t in trips if normalize_rider_id(t.rider_id) == rider), "trip_id")
        if request.trip_id:
            selected = [t for t in own_trips if t.trip_id == request.trip_id]
            if not selected:
                return finish(DisputeStatus.REJECTED, "Trip not found for the authenticated rider.")
            recorded_date = to_ist_date(selected[0].started_at)
            if target_date is not None and target_date != recorded_date:
                return finish(DisputeStatus.REJECTED, "Claimed date does not match the trip's recorded IST date.")
            target_date = recorded_date
        elif target_date is None:
            return finish(DisputeStatus.NEEDS_INFORMATION, "Provide a trip ID or the disputed IST calendar date.")
        else:
            selected = [t for t in own_trips if to_ist_date(t.started_at) == target_date]

        if not is_dispute_within_window(target_date, reference_date):
            return finish(DisputeStatus.REJECTED, "Dispute date is outside the last 7 days or is in the future.")

        ids = {t.trip_id for t in selected}
        # Include all postings for selected trips; only incentives use payout_date.
        # Unattributed day entries are retained so a daily audit cannot silently
        # ignore a credit or an unverifiable deduction.
        lines = _unique((p for p in payout_lines if p.trip_id in ids or (
            not request.trip_id and normalize_rider_id(p.rider_id) == rider and p.payout_date == target_date
        )), "line_id")
        for line in lines:
            if normalize_rider_id(line.rider_id) != rider:
                raise EvidenceError("A trip payout has inconsistent ownership; ops must reconcile it.")
            if type(line.amount) is not int:
                raise EvidenceError("Ledger amounts must be integer rupees.")
            if line.line_type not in set(PayoutLineType):
                raise EvidenceError("Unsupported ledger entry type; ops must reconcile it.")
            if line.line_type == PayoutLineType.DAILY_INCENTIVE:
                if line.trip_id:
                    raise EvidenceError("Daily incentive unexpectedly references a trip.")
            elif not line.trip_id:
                raise EvidenceError("Unattributed trip or penalty entry requires ops review.")
        # A posting for another known trip day is not part of this day's fare.
        known_ids = {t.trip_id for t in own_trips}
        if any(p.trip_id and p.trip_id not in known_ids for p in lines):
            raise EvidenceError("Ledger references an unverified trip; ops must reconcile it.")
        lines = [p for p in lines if p.trip_id in ids or (
            p.line_type == PayoutLineType.DAILY_INCENTIVE and p.payout_date == target_date
        )]

        trip_components = []
        for trip in selected:
            try:
                expected = calculate_expected_trip_payout(trip.status, trip.distance_km, trip.surge_multiplier)
                # Validate dispatch data even for cancellations/incentive counts.
                distance = normalize_distance_km(trip.distance_km)
                surge = Decimal(str(trip.surge_multiplier))
                if not surge.is_finite() or surge <= 0:
                    raise ValueError("Invalid surge")
            except (ValueError, ArithmeticError):
                raise EvidenceError("Invalid dispatch status, distance or surge; ops must review the trip.")
            trip_lines = [p for p in lines if p.trip_id == trip.trip_id]
            actual = sum(p.amount for p in trip_lines)
            penalties = [p for p in trip_lines if p.line_type == PayoutLineType.CANCELLATION_PENALTY]
            penalty_expected = calculate_cancellation_penalty(trip.status)
            penalty_actual = sum(p.amount for p in penalties)
            detected = None
            if penalty_actual < penalty_expected:
                detected = ("duplicate_cancellation_penalty" if sum(p.amount < 0 for p in penalties) > 1
                            else "incorrect_cancellation_penalty")
            elif expected > actual and trip.status == TripStatus.COMPLETED:
                if actual == 0:
                    detected = "missing_trip_payout"
                elif surge > 1 and actual == calculate_trip_fare(trip.distance_km, 1):
                    detected = "missing_surge"
                else:
                    detected = "incorrect_trip_payout"
            trip_components.append({
                "trip_id": trip.trip_id, "ist_date": to_ist_date(trip.started_at).isoformat(),
                "status": trip.status, "distance_km": str(distance), "surge_multiplier": str(surge),
                "expected_amount": expected, "actual_amount": actual,
                "shortfall": max(0, expected - actual),
                "penalty_expected": penalty_expected, "penalty_actual": penalty_actual,
                "negative_penalty_count": sum(p.amount < 0 for p in penalties),
                "extra_penalty": max(0, penalty_expected - penalty_actual),
                "detected_discrepancy": detected,
                "payout_line_ids": [p.line_id for p in trip_lines],
            })

        completed = sum(t.status == TripStatus.COMPLETED for t in selected)
        incentive_expected = calculate_daily_incentive(completed)
        incentive_lines = [p for p in lines if p.line_type == PayoutLineType.DAILY_INCENTIVE]
        incentive_actual = sum(p.amount for p in incentive_lines)
        evidence.update({
            "trips": trip_components,
            "payout_lines": [{"line_id": p.line_id, "trip_id": p.trip_id,
                              "line_type": p.line_type, "amount": p.amount,
                              "payout_date": p.payout_date.isoformat()} for p in lines],
            "completed_trips_count": completed,
            "incentive_threshold": DAILY_INCENTIVE_THRESHOLD,
            "daily_incentive": {"expected_amount": incentive_expected,
                                "actual_amount": incentive_actual,
                                "eligible": completed >= DAILY_INCENTIVE_THRESHOLD},
        })

        if request.dispute_type in INCENTIVE_TYPES:
            expected, actual = incentive_expected, incentive_actual
            details = f"{completed} completed trips on {target_date} IST; incentive threshold is {DAILY_INCENTIVE_THRESHOLD}."
        elif request.dispute_type in PENALTY_TYPES:
            expected = sum(c["penalty_expected"] for c in trip_components)
            actual = sum(c["penalty_actual"] for c in trip_components)
            details = "Rider cancellations incur ₹10 each; customer cancellations and completed trips incur no cancellation penalty."
        else:
            if request.dispute_type == DisputeType.MISSING_SURGE:
                trip_components = [c for c in trip_components if c["status"] == TripStatus.COMPLETED and Decimal(c["surge_multiplier"]) > 1]
                details = "Only recorded surge multipliers above 1 apply to a missing-surge claim."
            else:
                details = "Trip fares use ₹25 + ₹6 per km beyond 2 km, multiplied by surge and rounded half up."
            expected = sum(c["expected_amount"] for c in trip_components)
            actual = sum(c["actual_amount"] for c in trip_components)
            if request.dispute_type == DisputeType.GENERAL_PAYOUT_DISPUTE and not request.trip_id:
                expected += incentive_expected
                actual += incentive_actual
                details += f" Includes daily incentive for {completed} completed trips (threshold {DAILY_INCENTIVE_THRESHOLD})."

        owed = max(0, expected - actual)
        if request.dispute_type in PENALTY_TYPES:
            # A prior compensating credit must not be refunded a second time.
            owed = min(owed, max(0, sum(c["expected_amount"] - c["actual_amount"] for c in trip_components)))
        evidence["calculated_shortfall"] = owed
        details += f" Expected ₹{expected}; actual ₹{actual}; verified shortfall ₹{owed}."
        if request.claimed_amount is not None:
            details += f" Claimed ₹{request.claimed_amount}; the claim does not determine the calculation."
        return finish(DisputeStatus.VALID if owed else DisputeStatus.REJECTED,
                      details + (" Underpayment verified." if owed else " No payable shortfall found."),
                      expected, actual, owed)
    except EvidenceError as exc:
        return finish(DisputeStatus.NEEDS_REVIEW, str(exc))
