"""Read internal ledger and deterministic financial calculations."""

from sqlalchemy import select
from app.agent.tools.base import DateInput, OptionalDateInput, TripInput, read_tool
from app.db.models import PayoutLine
from app.domain.dispute_types import DisputeType
from app.domain.payout_calculator import calculate_expected_trip_payout, calculate_daily_incentive
from app.domain.policy import TripStatus, is_dispute_within_window


def build_tools(context):
    def get_rider_payouts(date=None):
        if date is not None and not is_dispute_within_window(date, context.today):
            raise ValueError("Date outside the dispute window")
        with context.session_factory() as session:
            lines = session.scalars(select(PayoutLine).where(PayoutLine.rider_id == context.rider_id)
                                    .order_by(PayoutLine.payout_date, PayoutLine.line_id)).all()
            return [{"line_id": p.line_id, "trip_id": p.trip_id, "payout_date": p.payout_date.isoformat(),
                     "line_type": p.line_type, "amount": p.amount} for p in lines if (
                         p.payout_date == date if date else is_dispute_within_window(p.payout_date, context.today))]

    def calculate_trip_expected_payout(trip_id):
        with context.session_factory() as session:
            trip = context.own_trip(session, trip_id)
            return {"trip_id": trip_id, "expected_amount": calculate_expected_trip_payout(
                trip.status, trip.distance_km, trip.surge_multiplier)}

    def daily_incentive(date):
        with context.session_factory() as session:
            count = sum(t.status == TripStatus.COMPLETED for t in context.recent_trips(session, date))
            return {"date": date.isoformat(), "completed_trips_count": count,
                    "expected_amount": calculate_daily_incentive(count)}

    return [
        read_tool(context, "get_rider_payouts", "Read the authenticated rider's internal ledger, optionally for an IST date.", OptionalDateInput, get_rider_payouts),
        read_tool(context, "calculate_trip_expected_payout", "Calculate a trip's expected payout from owned dispatch data.", TripInput, calculate_trip_expected_payout),
        read_tool(context, "calculate_daily_incentive", "Calculate eligibility and expected incentive for completed owned trips on an IST date.", DateInput, daily_incentive),
        read_tool(context, "reconcile_trip_payout", "Reconcile an owned trip against internal payout lines; returns evidence and shortfall.", TripInput,
                  lambda trip_id: context.reconcile(DisputeType.GENERAL_PAYOUT_DISPUTE, trip_id=trip_id)),
        read_tool(context, "reconcile_daily_incentive", "Reconcile the daily incentive against completed trips and actual incentive entries.", DateInput,
                  lambda date: context.reconcile(DisputeType.INCORRECT_DAILY_INCENTIVE, date=date)),
        read_tool(context, "reconcile_cancellation_penalties", "Reconcile cancellation penalties and refunds for an owned IST calendar day.", DateInput,
                  lambda date: context.reconcile(DisputeType.INCORRECT_CANCELLATION_PENALTY, date=date)),
    ]
