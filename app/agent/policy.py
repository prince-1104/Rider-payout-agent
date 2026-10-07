"""Deterministic routing. No model output or prose participates in financial policy."""

from app.agent.state import Action, PolicyDecision
from app.domain.dispute_types import DisputeStatus
from app.services.payment_authorization import PaymentAction, authorize_dispute_payment


def decide(disputes, payments, attempts, now, *, ledger_available=True):
    decisions = []
    reserved_in_run = False
    for dispute in disputes:
        action, reason = Action.ESCALATION, "Evidence needs human review"
        amount = dispute.amount_owed
        if dispute.status == DisputeStatus.NEEDS_INFORMATION:
            action, reason = Action.ASK_FOR_INFO, "Provide a trip ID or the disputed IST date"
        elif dispute.status == DisputeStatus.REJECTED:
            action, reason = Action.NO_DISPUTE, "No verified payable shortfall"
        elif dispute.status == DisputeStatus.VALID:
            authorization = authorize_dispute_payment(dispute, dispute.rider_id, payments=payments,
                attempts=attempts, now=now, ledger_available=ledger_available, reserved_in_run=reserved_in_run)
            action = Action.NO_DISPUTE if authorization.action == PaymentAction.NO_PAYMENT else Action(authorization.action.value)
            reason = authorization.reason
            if authorization.action == PaymentAction.AUTO_PAY:
                reserved_in_run = True
        decisions.append(PolicyDecision(dispute_id=dispute.dispute_id, action=action, amount=amount, reason=reason))
    return decisions
