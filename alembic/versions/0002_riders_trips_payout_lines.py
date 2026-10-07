"""create riders, trips, payout_lines

Revision ID: 0002_riders_trips_payout_lines
Revises: 0001_inbound_messages
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_riders_trips_payout_lines"
down_revision: str | None = "0001_inbound_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "riders",
        sa.Column("rider_id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("city", sa.String(length=64), nullable=False),
        sa.Column("joined_on", sa.Date(), nullable=False),
    )

    op.create_table(
        "trips",
        sa.Column("trip_id", sa.String(length=32), primary_key=True),
        sa.Column(
            "rider_id",
            sa.String(length=32),
            sa.ForeignKey("riders.rider_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("distance_km", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("surge_multiplier", sa.Numeric(precision=4, scale=2), nullable=False),
    )
    op.create_index("ix_trips_rider_id", "trips", ["rider_id"])
    op.create_index("ix_trips_started_at", "trips", ["started_at"])
    op.create_index("ix_trips_status", "trips", ["status"])

    op.create_table(
        "payout_lines",
        sa.Column("line_id", sa.String(length=32), primary_key=True),
        sa.Column("payout_date", sa.Date(), nullable=False),
        sa.Column(
            "rider_id",
            sa.String(length=32),
            sa.ForeignKey("riders.rider_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_type", sa.String(length=32), nullable=False),
        sa.Column(
            "trip_id",
            sa.String(length=32),
            sa.ForeignKey("trips.trip_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("amount", sa.Integer(), nullable=False),
    )
    op.create_index("ix_payout_lines_payout_date", "payout_lines", ["payout_date"])
    op.create_index("ix_payout_lines_rider_id", "payout_lines", ["rider_id"])
    op.create_index("ix_payout_lines_line_type", "payout_lines", ["line_type"])
    op.create_index("ix_payout_lines_trip_id", "payout_lines", ["trip_id"])


def downgrade() -> None:
    op.drop_table("payout_lines")
    op.drop_table("trips")
    op.drop_table("riders")
