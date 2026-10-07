"""Persist ops rejection decisions.

Revision ID: 0007
Revises: 0006_message_workflow_leases
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_ops_decisions"
down_revision = "0006_message_workflow_leases"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ops_queue_items") as batch:
        batch.drop_constraint("ck_ops_queue_status", type_="check")
        batch.create_check_constraint("ck_ops_queue_status", "status IN ('pending', 'resolved', 'rejected')")
        batch.add_column(sa.Column("resolution_reason", sa.Text(), nullable=True))
    with op.batch_alter_table("payment_attempts") as batch:
        batch.drop_constraint("ck_payment_attempts_amount", type_="check")
        batch.create_check_constraint("ck_payment_attempts_amount", "amount > 0")
        batch.drop_constraint("uq_payment_attempts_rider_day", type_="unique")
        batch.add_column(sa.Column("ops_approved", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("uq_payment_attempts_rider_day_auto", "payment_attempts", ["rider_id", "payment_date"],
                    unique=True, sqlite_where=sa.text("ops_approved = 0"),
                    postgresql_where=sa.text("ops_approved = false"))


def downgrade():
    op.drop_index("uq_payment_attempts_rider_day_auto", table_name="payment_attempts")
    with op.batch_alter_table("ops_queue_items") as batch:
        batch.drop_column("resolution_reason")
        batch.drop_constraint("ck_ops_queue_status", type_="check")
        batch.create_check_constraint("ck_ops_queue_status", "status IN ('pending', 'resolved')")
    with op.batch_alter_table("payment_attempts") as batch:
        batch.drop_column("ops_approved")
        batch.create_unique_constraint("uq_payment_attempts_rider_day", ["rider_id", "payment_date"])
        batch.drop_constraint("ck_payment_attempts_amount", type_="check")
        batch.create_check_constraint("ck_payment_attempts_amount", "amount > 0 AND amount <= 200")
