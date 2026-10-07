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

from app.db.models import Conversation, OpsQueueItem, PaymentAttempt, StoredDispute
from app.domain.dispute import DisputeRequest
from app.domain.dispute_types import DisputeStatus, INCENTIVE_TYPES
from app.domain.policy import AUTO_PAYMENT_LIMIT_RUPEES, normalize_rider_id, to_ist_date
from app.integrations.payswift import PaySwiftClient, PaySwiftProtocolError
from app.services.dispute_service import DisputeService
from app.services.conversation_service import ConversationService
from app.services.ops_service import OpsService
from app.services.payment_authorization import PaymentAction, PaymentAuthorization, authorize_dispute_payment
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
                 clock=lambda: datetime.now(timezone.utc), execution_guard=None):
        self.session_factory = session_factory
        self.payswift_factory = payswift_factory
        self.clock = clock
        self.execution_guard = execution_guard

    def pay_dispute(self, authenticated_rider_id: str, dispute_id: str) -> dict:
        """Compatibility entry point: pay/reconcile or raise; approvals remain pending."""
        return self._run(authenticated_rider_id, dispute_id, route=False)

    def process_dispute(self, authenticated_rider_id: str, dispute_id: str) -> dict:
        """Authorize afresh, execute AUTO_PAY or persist pending ops work. No amount input."""
        return self._run(authenticated_rider_id, dispute_id, route=True)

    def approve_ops_dispute(self, authenticated_rider_id: str, dispute_id: str) -> dict:
        """Execute a pending human-approved payment using a fresh engine amount."""
        return self._run(authenticated_rider_id, dispute_id, route=True, ops_approved=True)

    def _run(self, authenticated_rider_id, dispute_id, *, route, ops_approved=False):
        if not isinstance(authenticated_rider_id, str) or not authenticated_rider_id.strip():
            raise ValueError("Authenticated rider identity is required")
        authenticated_rider_id = normalize_rider_id(authenticated_rider_id)
        if len(authenticated_rider_id) > 32:
            raise ValueError("Authenticated rider identity exceeds the supported length")
        trace = TraceRecorder(self.session_factory, authenticated_rider_id)
        name = "ops_approved_payment" if ops_approved else (
            "application_payment_resolution" if route else "application_auto_payment")
        identifier = trace.start(name, {"dispute_id": dispute_id}, kind="decision")
        try:
            output = self._pay(authenticated_rider_id, dispute_id, route=route, ops_approved=ops_approved)
        except Exception as exc:
            trace.finish(identifier, error=exc)
            raise
        trace.finish(identifier, output=output)
        return output

    @staticmethod
    def _receipt(attempt):
        return {"attempt_id": attempt.attempt_id, "amount": attempt.amount,
                "status": attempt.status, "payout_id": attempt.payout_id}

    def _authorization_outcome(self, session, dispute, authorization, *, route):
        output = authorization.model_dump(mode="json")
        if authorization.action == PaymentAction.OPS_APPROVAL or (route and authorization.action == PaymentAction.ESCALATION):
            output["ops_record"] = OpsService(session).enqueue(dispute.rider_id, dispute.conversation_id,
                dispute_id=dispute.dispute_id, message_id=f"payment:{dispute.dispute_id}",
                kind="approval" if authorization.action == PaymentAction.OPS_APPROVAL else "escalation",
                reason=authorization.reason)
        elif route and authorization.action == PaymentAction.NO_PAYMENT:
            OpsService(session).mark_resolved(dispute.rider_id, dispute.dispute_id)
        session.commit()
        if not route:
            raise PaymentPolicyDenied(authorization.reason)
        return output

    def _pay(self, rider_id, dispute_id, *, route=False, ops_approved=False):
        if self.execution_guard:
            self.execution_guard()
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
                try:
                    payments = list_rider_payments(provider, rider_id)
                except Exception:
                    if not route:
                        raise
                    return self._authorization_outcome(session, dispute, PaymentAuthorization(
                        action=PaymentAction.ESCALATION, amount=0,
                        reason="PaySwift evidence is unavailable; reconciliation is required"), route=route)
                existing = next((a for a in attempts if a.dispute_id == dispute_id), None)
                if existing is not None:
                    matches = [p for p in payments if p.reference == existing.reference]
                    if len(matches) == 1 and matches[0].amount == existing.amount and matches[0].status == "processed":
                        existing.status = "processed"
                        existing.payout_id = matches[0].payout_id
                    else:
                        existing.status = "unknown"
                    session.commit()
                    if route:
                        if existing.status == "processed":
                            output = PaymentAuthorization(action=PaymentAction.NO_PAYMENT, amount=0,
                                reason="This dispute has already been paid according to PaySwift").model_dump(mode="json")
                            output["payment"] = self._receipt(existing)
                            OpsService(session).mark_resolved(rider_id, dispute_id)
                            session.commit()
                            return output
                        return self._authorization_outcome(session, dispute, PaymentAuthorization(
                            action=PaymentAction.ESCALATION, amount=0,
                            reason="Existing payment outcome requires provider reconciliation"), route=route)
                    return self._receipt(existing)  # No second POST, even if ledger says missing.
                if not route and (not dispute.is_active or dispute.status != DisputeStatus.VALID.value):
                    raise PaymentPolicyDenied("Only an active verified dispute can be paid")
                if route and (not dispute.is_active or dispute.status == DisputeStatus.NEEDS_REVIEW.value):
                    return self._authorization_outcome(session, dispute, PaymentAuthorization(
                        action=PaymentAction.ESCALATION, amount=0,
                        reason="Inactive or suspicious dispute requires ops review"), route=route)
                try:
                    claim = DisputeRequest.model_validate(dispute.claim)
                except ValidationError:
                    if not route:
                        raise
                    return self._authorization_outcome(session, dispute, PaymentAuthorization(
                        action=PaymentAction.ESCALATION, amount=0,
                        reason="Malformed persisted claim requires ops review"), route=route)
                calculated = DisputeService(session).evaluate(rider_id, claim, reference_date=now)
                # Approval records must use the fresh engine result, not a cached
                # balance, model amount or the rider's requested amount.
                ConversationService(session).record_dispute_result(rider_id, conversation_id=dispute.conversation_id,
                    result=calculated, expected_revision=dispute.revision)
                pending = session.scalar(select(OpsQueueItem).where(OpsQueueItem.dispute_id == dispute_id,
                    OpsQueueItem.rider_id == rider_id, OpsQueueItem.status == "pending"))
                if ops_approved and (pending is None or pending.type != "approval"):
                    raise PaymentPolicyDenied("A pending approval is required for ops payment execution")
                pending_review = (PaymentAction.OPS_APPROVAL if pending.type == "approval" else PaymentAction.ESCALATION) if pending else None
                reference = f"quickdrop:{rider_id}:{dispute_id}"
                provider_matches = [p for p in payments if p.reference == reference]
                if provider_matches:
                    if len(provider_matches) == 1 and provider_matches[0].status == "processed" \
                            and provider_matches[0].amount == calculated.amount_owed and calculated.amount_owed > 0:
                        matched = provider_matches[0]
                        attempt = PaymentAttempt(attempt_id=str(uuid4()), dispute_id=dispute_id,
                            rider_id=rider_id, payment_date=to_ist_date(matched.created_at),
                            amount=matched.amount, scope={"date": calculated.dispute_date.isoformat(),
                                "trip_id": calculated.trip_id, "type": calculated.dispute_type.value},
                            reference=reference, idempotency_key=str(uuid4()), status="processed",
                            payout_id=matched.payout_id, ops_approved=True)
                        session.add(attempt)
                        ConversationService(session).resolve_dispute(rider_id,
                            conversation_id=dispute.conversation_id, dispute_id=dispute_id,
                            expected_revision=dispute.revision)
                        OpsService(session).mark_resolved(rider_id, dispute_id)
                        session.commit()
                        return {"action": PaymentAction.NO_PAYMENT.value,
                            "amount": 0, "reason": "Existing PaySwift payout reconciled",
                            "payment": self._receipt(attempt)}
                    return self._authorization_outcome(session, dispute, PaymentAuthorization(
                        action=PaymentAction.ESCALATION, amount=0,
                        reason="PaySwift shows an existing or inconsistent payout for this dispute"), route=route)
                authorization = authorize_dispute_payment(calculated, rider_id, payments=payments, attempts=attempts,
                    now=now, pending_review=None if ops_approved else pending_review)
                if ops_approved and pending is not None and authorization.action in {
                    PaymentAction.AUTO_PAY, PaymentAction.OPS_APPROVAL,
                }:
                    # Explicit ops approval may bypass the automatic amount/day cap,
                    # but not the freshly calculated amount or any ledger anomaly.
                    pending.amount = calculated.amount_owed
                    authorization = PaymentAuthorization(action=PaymentAction.OPS_APPROVAL,
                        amount=calculated.amount_owed, reason="Ops approved the freshly verified shortfall")
                if authorization.action != PaymentAction.AUTO_PAY and not (
                    ops_approved and authorization.action == PaymentAction.OPS_APPROVAL
                ):
                    return self._authorization_outcome(session, dispute, authorization, route=route)
                amount = authorization.amount
                scope = {"date": calculated.dispute_date.isoformat(), "trip_id": calculated.trip_id,
                         "type": calculated.dispute_type.value}
                attempt = PaymentAttempt(attempt_id=str(uuid4()), dispute_id=dispute_id, rider_id=rider_id,
                    payment_date=to_ist_date(now), amount=amount, scope=scope,
                    reference=f"quickdrop:{rider_id}:{dispute_id}", idempotency_key=str(uuid4()), status="reserved",
                    ops_approved=ops_approved)
                session.add(attempt)
                try:
                    session.commit()  # Durable before the external side effect.
                except IntegrityError as exc:
                    session.rollback()
                    raise PaymentPolicyDenied("Another payment already reserved this dispute or rider/day") from exc
                attempt_id, reference, key = attempt.attempt_id, attempt.reference, attempt.idempotency_key

            try:
                # Check ownership/deadline again after the durable reservation and
                # immediately before money can move. Never ask the model.
                if self.execution_guard:
                    self.execution_guard()
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
                    if route:
                        stored = session.get(StoredDispute, dispute_id)
                        return self._authorization_outcome(session, stored, PaymentAuthorization(
                            action=PaymentAction.ESCALATION, amount=0,
                            reason="Payment outcome is unknown; reconcile PaySwift before further action"), route=route)
                raise PaymentOutcomeUnknown("Payment outcome is unknown; reconcile the persisted reference before further action") from exc
            with self.session_factory() as session:
                attempt = session.get(PaymentAttempt, attempt_id)
                attempt.status, attempt.payout_id = "processed", result["payout_id"]
                if route:
                    stored = session.get(StoredDispute, dispute_id)
                    ConversationService(session).resolve_dispute(rider_id, conversation_id=stored.conversation_id,
                        dispute_id=dispute_id, expected_revision=stored.revision)
                    OpsService(session).mark_resolved(rider_id, dispute_id)
                session.commit()
                if route:
                    return {**authorization.model_dump(mode="json"), "payment": self._receipt(attempt)}
                return self._receipt(attempt)
