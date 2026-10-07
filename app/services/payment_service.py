"""Application-only auto-payment gate. Never register this service as an LLM tool.

Recompute a persisted owned dispute, reconcile the provider ledger, enforce policy,
then commit a durable reservation *before* POST. Uncertain outcomes are held for
reconciliation, never automatically retried. Unattributed provider payments fail
closed because their effect on remaining shortfall cannot be safely inferred.
"""

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Conversation, PaymentAttempt, StoredDispute
from app.domain.dispute import DisputeRequest
from app.domain.dispute_types import DisputeStatus, INCENTIVE_TYPES
from app.domain.policy import AUTO_PAYMENT_LIMIT_RUPEES, normalize_rider_id, to_ist_date
from app.integrations.payswift import PaySwiftClient, PaySwiftProtocolError
from app.services.dispute_service import DisputeService
from app.services.trace import TraceRecorder


class PaymentPolicyDenied(ValueError):
    """The application could not establish permission for an automatic payment."""


class PaymentOutcomeUnknown(RuntimeError):
    """Payment may have moved money; reconcile its stable reference, do not retry blindly."""


class ProviderPayment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payout_id: str = Field(min_length=1)
    rider_id: str = Field(min_length=1)
    amount: StrictInt = Field(gt=0)
    reference: str | None = None
    status: str = Field(min_length=1)
    created_at: AwareDatetime


def list_rider_payments(client, rider_id: str) -> list[ProviderPayment]:
    payload = client.list_payouts(rider_id)
    if isinstance(payload, dict):
        records = payload.get("payouts", payload.get("items"))
        count = payload.get("count")
        if count is not None and (type(count) is not int or not isinstance(records, list) or count != len(records)):
            raise PaySwiftProtocolError("PaySwift ledger is incomplete or inconsistent")
    else:
        records = payload
    if not isinstance(records, list):
        raise PaySwiftProtocolError("PaySwift ledger must contain a complete list of payouts")
    try:
        payments = [ProviderPayment.model_validate(record) for record in records]
    except ValidationError as exc:
        raise PaySwiftProtocolError("PaySwift ledger contains invalid payment evidence") from exc
    if any(p.rider_id != rider_id for p in payments):
        raise PaySwiftProtocolError("PaySwift ledger contains inconsistent rider ownership")
    ids = [p.payout_id for p in payments]
    if len(set(ids)) != len(ids):
        raise PaySwiftProtocolError("PaySwift ledger contains duplicate payout identifiers")
    return payments


def auto_payment_eligibility(amount, payments, attempts, now: datetime) -> dict:
    today = to_ist_date(now)
    reason = "Eligible under amount/day rules; this is advisory and does not authorize payment"
    if type(amount) is not int or amount <= 0:
        reason = "Only positive integer-rupee shortfalls can be paid"
    elif amount > AUTO_PAYMENT_LIMIT_RUPEES:
        reason = "Amount exceeds ₹200; ops approval is required"
    elif any(to_ist_date(p.created_at) > today for p in payments):
        reason = "Provider ledger contains future-dated payments; review is required"
    elif any(p.status != "processed" for p in payments):
        reason = "Provider payment outcome is unresolved; reconcile before paying"
    elif any(to_ist_date(p.created_at) == today for p in payments):
        reason = "Rider already received a payment today; ops approval is required"
    elif any(a.status != "processed" for a in attempts):
        reason = "An application payment attempt is unresolved; reconcile before paying"
    elif any(a.payment_date == today for a in attempts):
        reason = "Today's application payment slot is already reserved; ops approval is required"
    return {"eligible": reason.startswith("Eligible"), "reason": reason,
            "limit": AUTO_PAYMENT_LIMIT_RUPEES, "date": today.isoformat()}


def scopes_overlap(first: dict, second: dict) -> bool:
    if first["date"] != second["date"]:
        return False
    first_incentive = first["type"] in {t.value for t in INCENTIVE_TYPES}
    second_incentive = second["type"] in {t.value for t in INCENTIVE_TYPES}
    if first_incentive and second_incentive:
        return True
    if (first_incentive and second["trip_id"]) or (second_incentive and first["trip_id"]):
        return False
    return not first["trip_id"] or not second["trip_id"] or first["trip_id"] == second["trip_id"]


class PaymentService:
    """Only trusted application code supplies rider/dispute IDs. No amount argument."""

    def __init__(self, session_factory, *, payswift_factory=PaySwiftClient,
                 clock=lambda: datetime.now(timezone.utc)):
        self.session_factory = session_factory
        self.payswift_factory = payswift_factory
        self.clock = clock

    def pay_dispute(self, authenticated_rider_id: str, dispute_id: str) -> dict:
        if not isinstance(authenticated_rider_id, str) or not authenticated_rider_id.strip():
            raise ValueError("Authenticated rider identity is required")
        authenticated_rider_id = normalize_rider_id(authenticated_rider_id)
        if len(authenticated_rider_id) > 32:
            raise ValueError("Authenticated rider identity exceeds the supported length")
        trace = TraceRecorder(self.session_factory, authenticated_rider_id)
        identifier = trace.start("application_auto_payment", {"dispute_id": dispute_id}, kind="decision")
        try:
            output = self._pay(authenticated_rider_id, dispute_id)
        except Exception as exc:
            trace.finish(identifier, error=exc)
            raise
        trace.finish(identifier, output=output)
        return output

    @staticmethod
    def _receipt(attempt):
        return {"attempt_id": attempt.attempt_id, "amount": attempt.amount,
                "status": attempt.status, "payout_id": attempt.payout_id}

    def _pay(self, rider_id, dispute_id):
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Application clock must be timezone-aware")
        with self.payswift_factory() as provider:
            with self.session_factory() as session:
                # Serialize policy decisions for a rider on PostgreSQL. Database
                # uniqueness provides the final safeguard on all supported engines.
                conversation = session.scalar(select(Conversation).where(Conversation.rider_id == rider_id).with_for_update())
                dispute = session.scalar(select(StoredDispute).where(
                    StoredDispute.dispute_id == dispute_id, StoredDispute.rider_id == rider_id).with_for_update())
                if conversation is None or dispute is None:
                    raise PaymentPolicyDenied("Dispute not found for the authenticated rider")
                attempts = session.scalars(select(PaymentAttempt).where(PaymentAttempt.rider_id == rider_id)).all()
                payments = list_rider_payments(provider, rider_id)
                existing = next((a for a in attempts if a.dispute_id == dispute_id), None)
                if existing is not None:
                    matches = [p for p in payments if p.reference == existing.reference]
                    if len(matches) == 1 and matches[0].amount == existing.amount and matches[0].status == "processed":
                        existing.status = "processed"
                        existing.payout_id = matches[0].payout_id
                    else:
                        existing.status = "unknown"
                    session.commit()
                    return self._receipt(existing)  # No second POST, even if ledger says missing.
                if not dispute.is_active or dispute.status != DisputeStatus.VALID.value:
                    raise PaymentPolicyDenied("Only an active verified dispute can be paid")
                calculated = DisputeService(session).evaluate(rider_id, DisputeRequest.model_validate(dispute.claim), reference_date=now)
                if calculated.status != DisputeStatus.VALID or calculated.rider_id != rider_id:
                    raise PaymentPolicyDenied("Current dispatch/ledger data does not verify a payable shortfall")
                amount = calculated.amount_owed
                eligibility = auto_payment_eligibility(amount, payments, attempts, now)
                if not eligibility["eligible"]:
                    raise PaymentPolicyDenied(eligibility["reason"])
                scope = {"date": calculated.dispute_date.isoformat(), "trip_id": calculated.trip_id,
                         "type": calculated.dispute_type.value}
                by_reference = {a.reference: a for a in attempts}
                for payment in payments:
                    previous = by_reference.get(payment.reference)
                    if previous is None or previous.amount != payment.amount:
                        raise PaymentPolicyDenied("Prior provider credit is unattributed; ops must reconcile the remaining balance")
                    if scopes_overlap(scope, previous.scope):
                        raise PaymentPolicyDenied("A prior provider credit overlaps this dispute; ops must reconcile the remaining shortfall")
                # Local processed records missing from the provider ledger are not proof of payment.
                if any(not any(p.reference == a.reference for p in payments) for a in attempts):
                    raise PaymentPolicyDenied("Application and provider ledgers disagree; reconciliation is required")
                attempt = PaymentAttempt(attempt_id=str(uuid4()), dispute_id=dispute_id, rider_id=rider_id,
                    payment_date=to_ist_date(now), amount=amount, scope=scope,
                    reference=f"quickdrop:{rider_id}:{dispute_id}", idempotency_key=str(uuid4()), status="reserved")
                session.add(attempt)
                try:
                    session.commit()  # Durable before the external side effect.
                except IntegrityError as exc:
                    session.rollback()
                    raise PaymentPolicyDenied("Another payment already reserved this dispute or rider/day") from exc
                attempt_id, reference, key = attempt.attempt_id, attempt.reference, attempt.idempotency_key

            try:
                result = provider.create_payout(rider_id, amount, reference, key)
                if result.get("status") != "processed" or any(
                    field in result and result[field] != value
                    for field, value in [("rider_id", rider_id), ("amount", amount), ("reference", reference)]
                ):
                    raise PaySwiftProtocolError("Payment acknowledgement is not a matching processed payment")
            except Exception as exc:
                with self.session_factory() as session:
                    attempt = session.get(PaymentAttempt, attempt_id)
                    attempt.status = "unknown"
                    session.commit()
                raise PaymentOutcomeUnknown("Payment outcome is unknown; reconcile the persisted reference before further action") from exc
            with self.session_factory() as session:
                attempt = session.get(PaymentAttempt, attempt_id)
                attempt.status, attempt.payout_id = "processed", result["payout_id"]
                session.commit()
                return self._receipt(attempt)
