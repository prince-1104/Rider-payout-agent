"""Durable message and conversation processing leases.

Revision ID: 0006
Revises: 0005_agent_ops_queue
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_message_workflow_leases"
down_revision = "0005_agent_ops_queue"
branch_labels = None
depends_on = None


def upgrade():
    for table, prefix in [("inbound_messages", "processing"), ("conversations", "workflow")]:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(f"{prefix}_token", sa.String(36), nullable=True))
            batch.add_column(sa.Column(f"{prefix}_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("inbound_messages", sa.Column("workflow_state", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("inbound_messages", "workflow_state")
    for table, prefix in [("conversations", "workflow"), ("inbound_messages", "processing")]:
        with op.batch_alter_table(table) as batch:
            batch.drop_column(f"{prefix}_expires_at")
            batch.drop_column(f"{prefix}_token")
