"""Deterministic, application-only payment authorization. Never bind to an LLM.

The optional keyword arguments are trusted server evidence/test dependencies, not
model inputs. Execution always reloads and recalculates the owned dispute itself.
Without supplied provider evidence, authorization reads PaySwift's rider ledger.
"""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from app.domain.dispute import Dispute
from app.domain.dispute_types import DisputeStatus, DisputeType
from app.domain.policy import AUTO_PAYMENT_LIMIT_RUPEES, is_dispute_within_window, normalize_rider_id, to_ist_date
from app.integrations.payswift import PaySwiftClient


class PaymentAction(str, Enum):
    AUTO_PAY = "AUTO_PAY"
    OPS_APPROVAL = "OPS_APPROVAL"
    ESCALATION = "ESCALATION"
    NO_PAYMENT = "NO_PAYMENT"


class PaymentAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: PaymentAction
    amount: StrictInt = Field(ge=0)
    reason: str

    @model_validator(mode="after")
    def check_amount(self):
        payable = self.action in {PaymentAction.AUTO_PAY, PaymentAction.OPS_APPROVAL}
        if payable != (self.amount > 0):
            raise ValueError("Only auto-payment or approval can carry a positive amount")
        if self.action == PaymentAction.AUTO_PAY and self.amount > AUTO_PAYMENT_LIMIT_RUPEES:
            raise ValueError("Auto-payment exceeds the policy limit")
        return self


def authorize_dispute_payment(dispute, rider_id, *, amount=None, payments=None,
                              attempts=(), now=None, ledger_available=True,
                              reserved_in_run=False, pending_review=None):
    """Return an immutable routing result using verified shortfall and PaySwift.

    ``amount`` defaults to the engine's amount_owed. It exists only for validating
    an explicit server-side proposal; the money-moving service accepts no amount.
    NO_PAYMENT/ESCALATION always authorize zero. An approval never executes money.
    """
    def result(action, reason, authorized=0):
        return PaymentAuthorization(action=action, amount=authorized, reason=reason)

    try:
        if not isinstance(rider_id, str) or not rider_id.strip():
            raise ValueError("Authenticated identity required")
        rider = normalize_rider_id(rider_id)
        data = dispute.model_dump() if isinstance(dispute, BaseModel) else dispute
        verified = Dispute.model_validate(data)
        if normalize_rider_id(verified.rider_id) != rider:
            raise ValueError("Dispute does not belong to authenticated rider")
    except (ValueError, TypeError, ValidationError):
        return result(PaymentAction.ESCALATION, "Dispute identity or financial evidence is invalid")
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return result(PaymentAction.ESCALATION, "A trusted timezone-aware clock is required")
    if verified.dispute_type == DisputeType.UNKNOWN or verified.status == DisputeStatus.NEEDS_REVIEW:
        return result(PaymentAction.ESCALATION, "Unknown or suspicious dispute requires ops review")
    requested = verified.amount_owed if amount is None else amount
    if type(requested) is not int:
        return result(PaymentAction.ESCALATION, "Payment amount must be integer rupees")
    if requested <= 0:
        return result(PaymentAction.NO_PAYMENT, "No positive payment amount")
    if requested > verified.amount_owed:
        return result(PaymentAction.ESCALATION, "Requested payment exceeds amount_owed; payment rejected")
    if verified.status != DisputeStatus.VALID:
        return result(PaymentAction.NO_PAYMENT, "No verified payable shortfall")
    if verified.dispute_date is None or not is_dispute_within_window(verified.dispute_date, now):
        return result(PaymentAction.ESCALATION, "Dispute requires a current verified IST date")
    if not ledger_available:
        return result(PaymentAction.ESCALATION, "Provider evidence is unavailable; reconciliation is required")
    if payments is None:
        # Import lazily to keep the provider protocol adapter in one place.
        from app.services.payment_service import list_rider_payments
        try:
            with PaySwiftClient() as provider:
                payments = list_rider_payments(provider, rider)
        except Exception:
            return result(PaymentAction.ESCALATION, "Provider evidence is unavailable; reconciliation is required")
    today = to_ist_date(now)
    existing = next((a for a in attempts if a.dispute_id == verified.dispute_id), None)
    if existing is not None:
        matches = [p for p in payments if p.reference == existing.reference]
        if len(matches) == 1 and matches[0].amount == existing.amount and matches[0].status == "processed":
            return result(PaymentAction.NO_PAYMENT, "This dispute has already been paid according to PaySwift")
        return result(PaymentAction.ESCALATION, "Existing payment outcome requires provider reconciliation")
    if any(p.status != "processed" or to_ist_date(p.created_at) > today for p in payments):
        return result(PaymentAction.ESCALATION, "Provider ledger has unresolved or future-dated payments")
    if any(a.status != "processed" for a in attempts):
        return result(PaymentAction.ESCALATION, "An application payment outcome requires reconciliation")
    by_reference = {a.reference: a for a in attempts}
    if any(p.reference in by_reference and by_reference[p.reference].amount != p.amount for p in payments):
        return result(PaymentAction.ESCALATION, "Application and provider amounts disagree; reconciliation is required")
    if any(not any(p.reference == a.reference for p in payments) for a in attempts):
        return result(PaymentAction.ESCALATION, "Application and provider ledgers disagree; reconciliation is required")
    from app.services.payment_service import scopes_overlap
    scope = {"date": verified.dispute_date.isoformat(), "trip_id": verified.trip_id, "type": verified.dispute_type.value}
    if any(p.reference in by_reference and scopes_overlap(scope, by_reference[p.reference].scope) for p in payments):
        return result(PaymentAction.ESCALATION, "A prior provider credit overlaps this dispute; reconcile the remaining shortfall")
    if any(p.reference not in by_reference and to_ist_date(p.created_at) != today for p in payments):
        return result(PaymentAction.ESCALATION, "Prior provider credit is unattributed; reconcile the remaining balance")
    if pending_review == PaymentAction.ESCALATION:
        return result(PaymentAction.ESCALATION, "Pending review requires explicit ops action")
    if pending_review == PaymentAction.OPS_APPROVAL:
        return result(PaymentAction.OPS_APPROVAL, "Pending approval requires explicit ops action; it cannot execute automatically", requested)
    # Conservatively route any processed credit today to ops. Provider references
    # do not reliably distinguish manual/external credits from automatic ones.
    if requested > AUTO_PAYMENT_LIMIT_RUPEES or verified.amount_owed > AUTO_PAYMENT_LIMIT_RUPEES:
        return result(PaymentAction.OPS_APPROVAL, "Amount exceeds ₹200; ops approval is required", requested)
    if reserved_in_run or any(to_ist_date(p.created_at) == today for p in payments):
        return result(PaymentAction.OPS_APPROVAL, "Rider already received a payment today; ops approval is required", requested)
    if any(a.payment_date == today for a in attempts):
        return result(PaymentAction.OPS_APPROVAL, "Today's payment slot is reserved; ops approval is required", requested)
    return result(PaymentAction.AUTO_PAY, "Verified shortfall satisfies automatic payment policy", requested)
