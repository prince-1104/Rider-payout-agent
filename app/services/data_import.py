"""Idempotent CSV importer for riders, trips, and payout lines."""

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_session_factory
from app.db.models import PayoutLine, Rider, Trip
from app.domain.policy import normalize_rider_id


def import_riders_from_csv(session: Session, csv_path: Path) -> tuple[int, int]:
    """Import riders from CSV file idempotently.

    Returns (inserted_count, updated_or_existing_count).
    """
    inserted = 0
    existing = 0
    seen_in_file: set[str] = set()

    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            norm_id = normalize_rider_id(row["rider_id"])
            if norm_id in seen_in_file:
                continue
            seen_in_file.add(norm_id)

            joined = date.fromisoformat(row["joined_on"])

            rider = session.get(Rider, norm_id)
            if rider is None:
                rider = Rider(
                    rider_id=norm_id,
                    name=row["name"].strip(),
                    city=row["city"].strip(),
                    joined_on=joined,
                )
                session.add(rider)
                inserted += 1
            else:
                rider.name = row["name"].strip()
                rider.city = row["city"].strip()
                rider.joined_on = joined
                existing += 1

    session.flush()
    return inserted, existing


def import_trips_from_csv(session: Session, csv_path: Path) -> tuple[int, int]:
    """Import trips from CSV file idempotently.

    Handles duplicate trip_id rows present within the source CSV.
    Returns (inserted_count, updated_or_existing_count).
    """
    inserted = 0
    existing = 0
    seen_in_file: set[str] = set()

    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trip_id = row["trip_id"].strip()
            if trip_id in seen_in_file:
                continue
            seen_in_file.add(trip_id)

            norm_rider = normalize_rider_id(row["rider_id"])

            raw_started = row["started_at"].strip()
            if raw_started.endswith("Z"):
                started_at = datetime.fromisoformat(raw_started.replace("Z", "+00:00"))
            else:
                started_at = datetime.fromisoformat(raw_started)
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)

            dist = Decimal(row["distance_km"].strip())
            surge = Decimal(row["surge_multiplier"].strip())
            status = row["status"].strip()

            trip = session.get(Trip, trip_id)
            if trip is None:
                trip = Trip(
                    trip_id=trip_id,
                    rider_id=norm_rider,
                    started_at=started_at,
                    status=status,
                    distance_km=dist,
                    surge_multiplier=surge,
                )
                session.add(trip)
                inserted += 1
            else:
                trip.rider_id = norm_rider
                trip.started_at = started_at
                trip.status = status
                trip.distance_km = dist
                trip.surge_multiplier = surge
                existing += 1

    session.flush()
    return inserted, existing


def import_payout_lines_from_csv(session: Session, csv_path: Path) -> tuple[int, int]:
    """Import payout lines from CSV file idempotently.

    Returns (inserted_count, updated_or_existing_count).
    """
    inserted = 0
    existing = 0
    seen_in_file: set[str] = set()

    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            line_id = row["line_id"].strip()
            if line_id in seen_in_file:
                continue
            seen_in_file.add(line_id)

            p_date = date.fromisoformat(row["payout_date"].strip())
            norm_rider = normalize_rider_id(row["rider_id"])
            l_type = row["line_type"].strip()
            raw_trip = row.get("trip_id", "").strip()
            trip_id = raw_trip if raw_trip else None
            amt = int(row["amount"].strip())

            line = session.get(PayoutLine, line_id)
            if line is None:
                line = PayoutLine(
                    line_id=line_id,
                    payout_date=p_date,
                    rider_id=norm_rider,
                    line_type=l_type,
                    trip_id=trip_id,
                    amount=amt,
                )
                session.add(line)
                inserted += 1
            else:
                line.payout_date = p_date
                line.rider_id = norm_rider
                line.line_type = l_type
                line.trip_id = trip_id
                line.amount = amt
                existing += 1

    session.flush()
    return inserted, existing


def import_all_data(
    data_dir: Path, session: Optional[Session] = None
) -> dict[str, tuple[int, int]]:
    """Import riders, trips, and payout lines in correct foreign-key order."""
    riders_csv = data_dir / "riders.csv"
    trips_csv = data_dir / "trips.csv"
    payout_lines_csv = data_dir / "payout_lines.csv"

    if not riders_csv.exists() or not trips_csv.exists() or not payout_lines_csv.exists():
        raise FileNotFoundError(f"Missing required CSV files in {data_dir}")

    own_session = session is None
    if own_session:
        session = get_session_factory()()

    results = {}
    try:
        r_ins, r_ext = import_riders_from_csv(session, riders_csv)
        results["riders"] = (r_ins, r_ext)

        t_ins, t_ext = import_trips_from_csv(session, trips_csv)
        results["trips"] = (t_ins, t_ext)

        p_ins, p_ext = import_payout_lines_from_csv(session, payout_lines_csv)
        results["payout_lines"] = (p_ins, p_ext)

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if own_session:
            session.close()

    return results


def main() -> None:
    """CLI entrypoint for data import."""
    parser = argparse.ArgumentParser(description="Import QuickDrop CSV data into database.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Directory containing riders.csv, trips.csv, payout_lines.csv",
    )
    args = parser.parse_args()

    print(f"Starting idempotent data import from {args.data_dir.resolve()}...")
    res = import_all_data(args.data_dir)
    print("Import completed successfully:")
    for entity, (ins, ext) in res.items():
        print(f"  {entity}: {ins} inserted, {ext} updated/existing (total: {ins + ext})")


if __name__ == "__main__":
    main()
