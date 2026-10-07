"""Read authenticated rider context; identity is bound, not a tool argument."""

from sqlalchemy import select
from app.db.models import Conversation, Rider, StoredDispute
from app.agent.tools.base import EmptyInput, read_tool


def build_tools(context):
    def get_rider_context():
        with context.session_factory() as session:
            rider = session.get(Rider, context.rider_id)
            conversation = session.scalar(select(Conversation).where(Conversation.rider_id == context.rider_id))
            disputes = session.scalars(select(StoredDispute).where(
                StoredDispute.rider_id == context.rider_id, StoredDispute.is_active.is_(True),
            ).order_by(StoredDispute.created_at, StoredDispute.dispute_id)).all()
            return {"rider_id": context.rider_id, "registered": rider is not None,
                    "profile": {"name": rider.name, "city": rider.city, "joined_on": rider.joined_on.isoformat()} if rider else None,
                    "conversation_id": conversation.conversation_id if conversation else None,
                    "state": conversation.state if conversation else None,
                    "active_dispute_ids": [d.dispute_id for d in disputes]}
    return [read_tool(context, "get_rider_context", "Read the authenticated rider's profile and conversation memory.", EmptyInput, get_rider_context)]
