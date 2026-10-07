"""Deterministic routing. No model output or prose participates in financial policy."""

from app.agent.state import Action, PolicyDecision
from app.domain.dispute_types import DisputeStatus
from app.domain.policy import AUTO_PAYMENT_LIMIT_RUPEES, to_ist_date
from app.services.payment_service import scopes_overlap


def decide(disputes, payments, attempts, now, *, ledger_available=True):
    decisions = []
    reserved_in_run = False
    today = to_ist_date(now)
    by_reference = {a.reference: a for a in attempts}
    for dispute in disputes:
        action, reason = Action.ESCALATION, "Evidence needs human review"
        amount = dispute.amount_owed
        if dispute.status == DisputeStatus.NEEDS_INFORMATION:
            action, reason = Action.ASK_FOR_INFO, "Provide a trip ID or the disputed IST date"
        elif dispute.status == DisputeStatus.REJECTED:
            action, reason = Action.NO_DISPUTE, "No verified payable shortfall"
        elif dispute.status == DisputeStatus.VALID:
            existing = next((a for a in attempts if a.dispute_id == dispute.dispute_id), None)
            if not ledger_available:
                reason = "Provider evidence is unavailable; reconciliation is required"
            elif existing is not None:
                matches = [p for p in payments if p.reference == existing.reference]
                if len(matches) == 1 and matches[0].amount == existing.amount and matches[0].status == "processed":
                    action, reason = Action.NO_DISPUTE, "This dispute has already been paid"
                else:
                    reason = "Existing payment outcome requires reconciliation"
            elif any(p.status != "processed" or to_ist_date(p.created_at) > today for p in payments):
                reason = "Provider ledger has unresolved or future-dated payments"
            elif any(a.status != "processed" for a in attempts):
                reason = "An application payment outcome requires reconciliation"
            elif any(p.reference not in by_reference or by_reference[p.reference].amount != p.amount for p in payments):
                reason = "Historical provider credits require balance reconciliation"
            elif any(not any(p.reference == a.reference for p in payments) for a in attempts):
                reason = "Application and provider ledgers disagree"
            elif any(scopes_overlap(
                {"date": dispute.dispute_date.isoformat(), "trip_id": dispute.trip_id, "type": dispute.dispute_type.value},
                by_reference[p.reference].scope,
            ) for p in payments):
                reason = "A prior credit overlaps this dispute; reconcile the remaining shortfall"
            elif amount > AUTO_PAYMENT_LIMIT_RUPEES:
                action, reason = Action.OPS_APPROVAL, "Verified shortfall exceeds the automatic payment limit"
            elif reserved_in_run or any(to_ist_date(p.created_at) == today for p in payments) or any(a.payment_date == today for a in attempts):
                action, reason = Action.OPS_APPROVAL, "Only one automatic payment is permitted per rider per IST day"
            else:
                action, reason = Action.AUTO_PAY, "Verified shortfall satisfies automatic payment policy"
                reserved_in_run = True
        decisions.append(PolicyDecision(dispute_id=dispute.dispute_id, action=action, amount=amount, reason=reason))
    return decisions
