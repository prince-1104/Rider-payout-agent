"""Read persisted disputes; mutations stay in the trusted application layer."""

from sqlalchemy import select
from app.agent.tools.base import DisputeInput, ToolAccessError, read_tool
from app.db.models import StoredDispute


def build_tools(context):
    def get_dispute_status(dispute_id):
        with context.session_factory() as session:
            dispute = session.scalar(select(StoredDispute).where(
                StoredDispute.dispute_id == dispute_id, StoredDispute.rider_id == context.rider_id))
            if dispute is None:
                raise ToolAccessError("Dispute not found for this rider")
            return {"dispute_id": dispute.dispute_id, "status": dispute.status, "is_active": dispute.is_active,
                    "revision": dispute.revision, "claim": dispute.claim,
                    "expected_amount": dispute.expected_amount, "actual_amount": dispute.actual_amount,
                    "amount_owed": dispute.amount_owed, "explanation": dispute.explanation,
                    "evidence": dispute.evidence}
    return [read_tool(context, "get_dispute_status", "Read an authenticated rider's persisted dispute and its investigation evidence.", DisputeInput, get_dispute_status)]
