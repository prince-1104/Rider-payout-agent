"""Persistent conversations, ordered turns and revisioned disputes.

Revision ID: 0003_conversation_state
Revises: 0002_riders_trips_payout_lines
"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0003_conversation_state"
down_revision = "0002_riders_trips_payout_lines"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]


def upgrade():
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(36), primary_key=True),
        sa.Column("rider_id", sa.String(32), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("rider_id", name="uq_conversations_rider_id"),
        sa.UniqueConstraint("conversation_id", "rider_id", name="uq_conversations_identity"),
        sa.CheckConstraint("message_count >= 0 AND version >= 0", name="ck_conversations_counters"),
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("rider_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id", "rider_id"],
                                ["conversations.conversation_id", "conversations.rider_id"],
                                ondelete="CASCADE", name="fk_conversation_messages_identity"),
        sa.UniqueConstraint("message_id", "role", name="uq_conversation_messages_vendor_role"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_conversation_messages_sequence"),
        sa.CheckConstraint("role IN ('rider', 'assistant')", name="ck_conversation_messages_role"),
        sa.CheckConstraint("sequence > 0", name="ck_conversation_messages_sequence"),
    )
    op.create_index("ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"])
    op.create_table(
        "disputes",
        sa.Column("dispute_id", sa.String(128), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("rider_id", sa.String(32), nullable=False),
        sa.Column("dispute_type", sa.String(64), nullable=False),
        sa.Column("trip_id", sa.String(32), nullable=True),
        sa.Column("dispute_date", sa.Date(), nullable=True),
        sa.Column("claimed_amount", sa.Integer(), nullable=True),
        sa.Column("expected_amount", sa.Integer(), nullable=True),
        sa.Column("actual_amount", sa.Integer(), nullable=True),
        sa.Column("amount_owed", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("claim", sa.JSON(), nullable=False),
        sa.Column("revisions", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.ForeignKeyConstraint(["conversation_id", "rider_id"],
                                ["conversations.conversation_id", "conversations.rider_id"],
                                ondelete="CASCADE", name="fk_disputes_identity"),
        sa.CheckConstraint("amount_owed >= 0", name="ck_disputes_nonnegative"),
        sa.CheckConstraint("amount_owed = 0 OR (status = 'valid' AND expected_amount IS NOT NULL "
                           "AND actual_amount IS NOT NULL AND amount_owed <= expected_amount - actual_amount)",
                           name="ck_disputes_verified_shortfall"),
    )
    op.create_index("ix_disputes_conversation_id", "disputes", ["conversation_id"])
    # Retain previously accepted messages and replies when upgrading an existing deployment.
    connection = op.get_bind()
    metadata = sa.MetaData()
    inbound = sa.Table("inbound_messages", metadata, autoload_with=connection)
    conversations = sa.Table("conversations", metadata, autoload_with=connection)
    messages = sa.Table("conversation_messages", metadata, autoload_with=connection)
    identities = {}
    counters = {}
    for row in connection.execute(sa.select(inbound).order_by(inbound.c.id)).mappings():
        rider = row["rider_id"]
        if rider not in identities:
            identities[rider] = str(uuid4())
            counters[rider] = 0
            connection.execute(conversations.insert().values(
                conversation_id=identities[rider], rider_id=rider, state={}, version=0,
                message_count=0, created_at=row["created_at"], updated_at=row["created_at"],
            ))
        for role, text in [("rider", row["text"]), ("assistant", row["reply"])]:
            if text is None:
                continue
            counters[rider] += 1
            connection.execute(messages.insert().values(
                conversation_id=identities[rider], rider_id=rider, message_id=row["message_id"],
                role=role, sequence=counters[rider], text=text, received_at=row["received_at"],
                created_at=row["created_at"],
            ))
        connection.execute(conversations.update().where(conversations.c.rider_id == rider).values(
            message_count=counters[rider], updated_at=row["created_at"],
        ))


def downgrade():
    op.drop_table("disputes")
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
