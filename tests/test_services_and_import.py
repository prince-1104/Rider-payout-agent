"""Tests for TripService, PayoutService, and data_import.py."""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import PayoutLine, Rider, Trip
from app.domain.policy import TripStatus
from app.services.data_import import import_all_data
from app.services.payout_service import audit_rider_day, audit_trip
from app.services.trip_service import get_trip_by_id, get_trips_for_rider_on_ist_date

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def test_unknown_trip_returns_none(session: Session) -> None:
    """Invalid or unknown trip returns None from trip_service and payout_service."""
    assert get_trip_by_id(session, "T_DOES_NOT_EXIST") is None
    assert audit_trip(session, "T_DOES_NOT_EXIST") is None


def test_trip_service_and_payout_service_with_seeded_data(session: Session) -> None:
    """Test manual trip creation and reconciliation via services."""
    rider = Rider(
        rider_id="R999",
        name="Test Rider",
        city="Bengaluru",
        joined_on=date(2026, 1, 1),
    )
    session.add(rider)

    # 2026-09-20 07:00:00 UTC is 2026-09-20 12:30:00 IST
    t1 = Trip(
        trip_id="T99901",
        rider_id="R999",
        started_at=datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc),
        status=TripStatus.COMPLETED,
        distance_km=Decimal("6.0"),
        surge_multiplier=Decimal("1.5"),
    )
    session.add(t1)

    p1 = PayoutLine(
        line_id="PL99901",
        payout_date=date(2026, 9, 20),
        rider_id="R999",
        line_type="trip",
        trip_id="T99901",
        amount=49,  # Underpaid! Expected 74
    )
    session.add(p1)
    session.commit()

    # Query trip
    fetched_trip = get_trip_by_id(session, "T99901")
    assert fetched_trip is not None
    assert fetched_trip.rider_id == "R999"

    # Query trips on date
    day_trips = get_trips_for_rider_on_ist_date(session, "R999", date(2026, 9, 20))
    assert len(day_trips) == 1
    assert day_trips[0].trip_id == "T99901"

    # Audit single trip
    reconciled = audit_trip(session, "T99901")
    assert reconciled is not None
    assert reconciled.expected_amount == 74
    assert reconciled.actual_paid_amount == 49
    assert reconciled.amount_owed == 25
    assert reconciled.discrepancy_type == "surge_missing"

    # Audit full day
    day_audit = audit_rider_day(session, "R999", date(2026, 9, 20), reference_date=date(2026, 9, 22))
    assert day_audit.is_within_policy_window is True
    assert day_audit.completed_trips_count == 1
    assert day_audit.total_owed == 25
    assert len(day_audit.trip_discrepancies) == 1


def test_idempotent_csv_data_import(session: Session) -> None:
    """Test loading data/ CSV exports into database, and verify idempotency."""
    # First import
    res1 = import_all_data(DATA_DIR, session=session)
    assert res1["riders"][0] > 0
    assert res1["trips"][0] > 0
    assert res1["payout_lines"][0] > 0

    riders_count_1 = session.scalar(select(func.count()).select_from(Rider))
    trips_count_1 = session.scalar(select(func.count()).select_from(Trip))
    lines_count_1 = session.scalar(select(func.count()).select_from(PayoutLine))

    assert riders_count_1 == 40
    assert trips_count_1 > 3000
    assert lines_count_1 > 3000

    # Second import must be 100% idempotent (0 new rows inserted)
    res2 = import_all_data(DATA_DIR, session=session)
    assert res2["riders"][0] == 0
    assert res2["trips"][0] == 0
    assert res2["payout_lines"][0] == 0

    assert session.scalar(select(func.count()).select_from(Rider)) == riders_count_1
    assert session.scalar(select(func.count()).select_from(Trip)) == trips_count_1
    assert session.scalar(select(func.count()).select_from(PayoutLine)) == lines_count_1

    # Verify known conversation scenario from imported data (R003 on 2026-09-20)
    day_audit = audit_rider_day(
        session, "R003", date(2026, 9, 20), reference_date=date(2026, 9, 22)
    )
    assert day_audit.is_within_policy_window is True
    assert any(d.trip_id == "T926334" and d.amount_owed == 25 for d in day_audit.trip_discrepancies)
