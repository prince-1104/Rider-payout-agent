"""create inbound_messages

Revision ID: 0001_inbound_messages
Revises:
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_inbound_messages"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inbound_messages",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("rider_id", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("message_id", name="uq_inbound_messages_message_id"),
    )
    op.create_index("ix_inbound_messages_rider_id", "inbound_messages", ["rider_id"])


def downgrade() -> None:
    op.drop_index("ix_inbound_messages_rider_id", table_name="inbound_messages")
    op.drop_table("inbound_messages")
