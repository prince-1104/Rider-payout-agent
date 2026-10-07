"""Policy routes verified results without any model-controlled authority."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.agent.policy import decide
from app.agent.state import Action
from app.domain.dispute import Dispute
from app.domain.dispute_types import DisputeStatus, DisputeType
from tests.test_agent_tools import NOW


def investigation(amount=25, *, identifier="D1", trip_id="T1", status=DisputeStatus.VALID):
    return Dispute(dispute_id=identifier, rider_id="R003", dispute_type=DisputeType.MISSING_SURGE,
        trip_id=trip_id, dispute_date=date(2026, 9, 20), claimed_amount=None,
        expected_amount=amount, actual_amount=0, amount_owed=amount,
        status=status, explanation="Deterministic result")


def attempt(**changes):
    fields = dict(dispute_id="OLD", reference="ref", amount=25, status="processed",
                  payment_date=date(2026, 9, 21), scope={"date": "2026-09-20", "trip_id": "OTHER", "type": "missing_surge"})
    return SimpleNamespace(**{**fields, **changes})


def payment(**changes):
    fields = dict(reference="ref", amount=25, status="processed", created_at=NOW - timedelta(days=1))
    return SimpleNamespace(**{**fields, **changes})


@pytest.mark.parametrize("amount,action", [(1, Action.AUTO_PAY), (200, Action.AUTO_PAY), (201, Action.OPS_APPROVAL)])
def test_policy_limit_comes_from_verified_result(amount, action):
    result = decide([investigation(amount)], [], [], NOW)[0]
    assert result.action == action and result.amount == amount


@pytest.mark.parametrize("status,action", [(DisputeStatus.REJECTED, Action.NO_DISPUTE),
    (DisputeStatus.NEEDS_INFORMATION, Action.ASK_FOR_INFO), (DisputeStatus.NEEDS_REVIEW, Action.ESCALATION)])
def test_nonpayable_statuses_never_authorize_money(status, action):
    result = decide([investigation(0, status=status)], [], [], NOW)[0]
    assert result.action == action and result.amount == 0


def test_only_first_valid_dispute_can_auto_pay_in_same_run():
    decisions = decide([investigation(), investigation(identifier="D2", trip_id="T2")], [], [], NOW)
    assert [d.action for d in decisions] == [Action.AUTO_PAY, Action.OPS_APPROVAL]


def test_existing_confirmed_payment_is_not_authorized_again():
    decisions = decide([investigation()], [payment()], [attempt(dispute_id="D1")], NOW)
    assert decisions[0].action == Action.NO_DISPUTE


def test_payment_today_requires_ops_approval_using_ist_calendar_day():
    # 18:30 UTC on September 21 is midnight IST on September 22.
    boundary = NOW.replace(day=21, hour=18, minute=30)
    result = decide([investigation()], [payment(created_at=boundary)], [attempt()], NOW)[0]
    assert result.action == Action.OPS_APPROVAL


@pytest.mark.parametrize("payments,attempts", [
    ([payment(status="pending")], [attempt()]),
    ([payment(created_at=NOW + timedelta(days=1))], [attempt()]),
    ([payment()], []),
    ([], [attempt()]),
    ([payment(amount=50)], [attempt()]),
    ([], [attempt(status="unknown")]),
    ([payment()], [attempt(scope={"date": "2026-09-20", "trip_id": "T1", "type": "missing_surge"})]),
])
def test_ambiguous_or_overlapping_provider_evidence_requires_review(payments, attempts):
    result = decide([investigation()], payments, attempts, NOW)[0]
    assert result.action == Action.ESCALATION


def test_unavailable_ledger_fails_closed():
    assert decide([investigation()], [], [], NOW, ledger_available=False)[0].action == Action.ESCALATION


def test_historical_confirmed_nonoverlapping_payment_allows_new_shortfall():
    assert decide([investigation()], [payment()], [attempt()], NOW)[0].action == Action.AUTO_PAY
