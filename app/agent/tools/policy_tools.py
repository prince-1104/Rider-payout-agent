"""Advisory read-only policy checks. These tools cannot authorize or move money."""

from sqlalchemy import select
from app.agent.tools.base import AmountInput, read_tool
from app.db.models import PaymentAttempt
from app.services.payment_service import auto_payment_eligibility, list_rider_payments


def build_tools(context):
    def check_auto_payment_eligibility(amount):
        with context.payswift_factory() as client:
            payments = list_rider_payments(client, context.rider_id)
        with context.session_factory() as session:
            attempts = session.scalars(select(PaymentAttempt).where(PaymentAttempt.rider_id == context.rider_id)).all()
            return auto_payment_eligibility(amount, payments, attempts, context.now)
    return [read_tool(context, "check_auto_payment_eligibility", "Advisory amount/day policy check using PaySwift; never authorizes or creates a payment.", AmountInput, check_auto_payment_eligibility)]
