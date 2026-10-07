"""Phase 7 application-created pending approvals and escalations."""

from alembic import op
import sqlalchemy as sa

revision = "0005_agent_ops_queue"
down_revision = "0004_tools_and_payments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("ops_queue_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("rider_id", sa.String(32), nullable=False),
        sa.Column("dispute_id", sa.String(128), sa.ForeignKey("disputes.dispute_id"), nullable=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["conversation_id", "rider_id"],
                                ["conversations.conversation_id", "conversations.rider_id"], name="fk_ops_queue_identity"),
        sa.CheckConstraint("(type = 'approval' AND amount IS NOT NULL AND amount > 0) OR "
                           "(type = 'escalation' AND amount IS NULL)", name="ck_ops_queue_amount"),
        sa.CheckConstraint("status IN ('pending', 'resolved')", name="ck_ops_queue_status"))
    op.create_index("ix_ops_queue_items_rider_id", "ops_queue_items", ["rider_id"])


def downgrade():
    op.drop_table("ops_queue_items")
