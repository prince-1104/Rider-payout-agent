"""Durable invocation traces and automatic payment reservations.

Revision ID: 0004_tools_and_payments
Revises: 0003_conversation_state
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_tools_and_payments"
down_revision = "0003_conversation_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("trace_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("invocation_id", sa.String(36), nullable=False),
        sa.Column("rider_id", sa.String(32), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("input", sa.JSON(), nullable=True), sa.Column("output", sa.JSON(), nullable=True))
    op.create_index("ix_trace_events_invocation_id", "trace_events", ["invocation_id"])
    op.create_index("ix_trace_events_rider_id", "trace_events", ["rider_id"])
    op.create_table("payment_attempts",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("dispute_id", sa.String(128), sa.ForeignKey("disputes.dispute_id"), nullable=False),
        sa.Column("rider_id", sa.String(32), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("reference", sa.String(256), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payout_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("dispute_id", name="uq_payment_attempts_dispute"),
        sa.UniqueConstraint("rider_id", "payment_date", name="uq_payment_attempts_rider_day"),
        sa.UniqueConstraint("idempotency_key", name="uq_payment_attempts_key"),
        sa.UniqueConstraint("reference", name="uq_payment_attempts_reference"),
        sa.CheckConstraint("amount > 0 AND amount <= 200", name="ck_payment_attempts_amount"))
    op.create_index("ix_payment_attempts_rider_id", "payment_attempts", ["rider_id"])


def downgrade():
    op.drop_table("payment_attempts")
    op.drop_table("trace_events")
