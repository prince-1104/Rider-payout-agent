"""PaySwift READ tools only. Payout creation is intentionally absent."""

from app.agent.tools.base import EmptyInput, read_tool
from app.domain.policy import to_ist_date
from app.services.payment_service import list_rider_payments


def build_tools(context):
    def get_payments_today():
        with context.payswift_factory() as client:
            payments = list_rider_payments(client, context.rider_id)
            return [p.model_dump(mode="json") for p in payments if to_ist_date(p.created_at) == context.today]
    return [read_tool(context, "get_payments_today", "Read the authenticated rider's PaySwift payments on today's IST calendar day.", EmptyInput, get_payments_today)]
