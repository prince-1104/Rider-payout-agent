"""Phase 9 policy boundaries and payment execution, using PaySwift HTTP mocks."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.db.models import OpsQueueItem, PaymentAttempt, PayoutLine, Rider, TraceEvent, Trip
from app.domain.dispute import Dispute, DisputeRequest
from app.domain.dispute_types import DisputeStatus, DisputeType
from app.services.conversation_service import ConversationService
from app.services.dispute_service import DisputeService
from app.services.payment_authorization import PaymentAction, PaymentAuthorization, authorize_dispute_payment
from app.services.payment_service import PaymentService, ProviderPayment
from tests.test_agent_tools import NOW
from tests.test_payment_service import sandbox, posts


def verified(amount=25, **changes):
    fields = dict(dispute_id="D9", rider_id="R003", dispute_type=DisputeType.GENERAL_PAYOUT_DISPUTE,
        trip_id="T9", dispute_date=date(2026, 9, 22), claimed_amount=None, expected_amount=amount,
        actual_amount=0, amount_owed=amount, status=DisputeStatus.VALID if amount else DisputeStatus.REJECTED,
        explanation="Verified by the deterministic engine")
    return Dispute(**{**fields, **changes})


@pytest.mark.parametrize("amount,action", [(0, PaymentAction.NO_PAYMENT), (1, PaymentAction.AUTO_PAY),
    (199, PaymentAction.AUTO_PAY), (200, PaymentAction.AUTO_PAY), (201, PaymentAction.OPS_APPROVAL),
    (500, PaymentAction.OPS_APPROVAL)])
def test_all_policy_amount_boundaries(amount, action):
    decision = authorize_dispute_payment(verified(amount), "R003", payments=[], now=NOW)
    assert decision.action == action and decision.amount == amount


@pytest.mark.parametrize("amount", [-1, 0])
def test_nonpositive_proposals_authorize_no_payment(amount):
    decision = authorize_dispute_payment(verified(), "R003", amount=amount, payments=[], now=NOW)
    assert decision.action == PaymentAction.NO_PAYMENT and decision.amount == 0


@pytest.mark.parametrize("owed,proposed", [(1, 2), (199, 200), (200, 201), (201, 999)])
def test_more_than_owed_is_rejected_before_limit_routing(owed, proposed):
    decision = authorize_dispute_payment(verified(owed), "R003", amount=proposed, payments=[], now=NOW)
    assert decision.action == PaymentAction.ESCALATION and decision.amount == 0
    assert "exceeds amount_owed" in decision.reason


@pytest.mark.parametrize("amount", [True, "25", 25.5])
def test_noninteger_proposals_are_not_authority(amount):
    decision = authorize_dispute_payment(verified(), "R003", amount=amount, payments=[], now=NOW)
    assert decision.action == PaymentAction.ESCALATION and decision.amount == 0


@pytest.mark.parametrize("dispute", [verified(dispute_type=DisputeType.UNKNOWN),
    verified(0, status=DisputeStatus.NEEDS_REVIEW), verified(rider_id="R005"),
    verified().model_copy(update={"amount_owed": 999}), {"amount_owed": 999}])
def test_unknown_suspicious_foreign_or_forged_disputes_escalate(dispute):
    decision = authorize_dispute_payment(dispute, "R003", payments=[], now=NOW)
    assert decision.action == PaymentAction.ESCALATION and decision.amount == 0


def test_repeat_payment_today_uses_provider_ist_day():
    payment = ProviderPayment(payout_id="P-OLD", rider_id="R003", amount=1, status="processed",
        reference="quickdrop:R003:OLD", created_at=NOW.replace(day=21, hour=18, minute=30))
    decision = authorize_dispute_payment(verified(), "R003", payments=[payment], now=NOW)
    assert decision.action == PaymentAction.OPS_APPROVAL


def test_large_owed_amount_cannot_be_split_to_bypass_approval():
    decision = authorize_dispute_payment(verified(500), "R003", amount=199, payments=[], now=NOW)
    assert decision.action == PaymentAction.OPS_APPROVAL and decision.amount <= 500


def test_overlapping_provider_credit_escalates_even_today_or_above_limit():
    payment = ProviderPayment(payout_id="OLD", rider_id="R003", amount=25, reference="ref",
                              status="processed", created_at=NOW)
    attempt = SimpleNamespace(dispute_id="PREVIOUS", reference="ref", amount=25, status="processed",
        payment_date=NOW.date(), scope={"date": NOW.date().isoformat(), "trip_id": "T9", "type": "general_payout_dispute"})
    for amount in [25, 201]:
        result = authorize_dispute_payment(verified(amount), "R003", payments=[payment], attempts=[attempt], now=NOW)
        assert result.action == PaymentAction.ESCALATION and result.amount == 0


def test_authorization_is_immutable_and_esc_or_no_pay_cannot_carry_money():
    decision = authorize_dispute_payment(verified(), "R003", payments=[], now=NOW)
    with pytest.raises(ValidationError):
        decision.action = PaymentAction.OPS_APPROVAL
    with pytest.raises(ValidationError):
        PaymentAuthorization(action=PaymentAction.AUTO_PAY, amount=201, reason="override")
    with pytest.raises(ValidationError):
        PaymentAuthorization(action=PaymentAction.ESCALATION, amount=25, reason="override")


def test_default_authorization_reads_payswift_not_local_tables(sandbox, monkeypatch):
    monkeypatch.setattr("app.services.payment_authorization.PaySwiftClient", sandbox.factory)
    decision = authorize_dispute_payment(verified(), "R003", now=NOW)
    assert decision.action == PaymentAction.AUTO_PAY
    assert [r.method for r in sandbox.calls] == ["GET"]
    assert sandbox.calls[0].url.path == "/v1/payouts"
    assert sandbox.calls[0].url.params["rider_id"] == "R003"


@pytest.fixture
def financial_case(session):
    session.add(Rider(rider_id="R003", name="Test rider", city="Test", joined_on=date(2026, 1, 1)))
    session.flush()

    def create(owed, identifier="D9"):
        trip_id = f"T-{identifier}"
        session.add(Trip(trip_id=trip_id, rider_id="R003", started_at=NOW, status="completed",
                         distance_km=90, surge_multiplier=1))
        session.flush()
        # ₹25 + ₹6 * (90 - 2) = ₹553, all money computed by the engine.
        session.add(PayoutLine(line_id=f"L-{identifier}", rider_id="R003", payout_date=NOW.date(),
                               line_type="trip", trip_id=trip_id, amount=553 - owed))
        session.flush()
        conversations = ConversationService(session)
        conversation = conversations.get_or_create_conversation("R003")
        claim = DisputeRequest(dispute_id=identifier, trip_id=trip_id)
        conversations.create_dispute("R003", conversation_id=conversation.conversation_id, claim=claim)
        calculated = DisputeService(session).evaluate("R003", claim, reference_date=NOW)
        conversations.record_dispute_result("R003", conversation_id=conversation.conversation_id,
                                            result=calculated, expected_revision=1)
        session.commit()
        assert calculated.amount_owed == owed
        return identifier
    return create


def service(session_factory, sandbox, now=NOW):
    return PaymentService(session_factory, payswift_factory=sandbox.factory, clock=lambda: now)


@pytest.mark.parametrize("amount,action", [(0, PaymentAction.NO_PAYMENT), (1, PaymentAction.AUTO_PAY),
    (199, PaymentAction.AUTO_PAY), (200, PaymentAction.AUTO_PAY), (201, PaymentAction.OPS_APPROVAL),
    (500, PaymentAction.OPS_APPROVAL)])
def test_payment_service_executes_only_authorized_boundaries(financial_case, session_factory, session, sandbox, amount, action):
    identifier = financial_case(amount)
    output = service(session_factory, sandbox).process_dispute("R003", identifier)
    assert output["action"] == action.value and output["amount"] == amount
    if action == PaymentAction.AUTO_PAY:
        attempt = session.scalars(select(PaymentAttempt)).one()
        assert attempt.amount == amount and attempt.payout_id == output["payment"]["payout_id"] == "P1"
        assert json.loads(posts(sandbox)[0].content)["amount"] == amount
        assert posts(sandbox)[0].headers["Idempotency-Key"] == attempt.idempotency_key
        assert not session.scalars(select(OpsQueueItem)).all()
    elif action == PaymentAction.OPS_APPROVAL:
        item = session.scalars(select(OpsQueueItem)).one()
        assert item.type == "approval" and item.amount == amount and item.status == "pending"
        assert not posts(sandbox) and not session.scalars(select(PaymentAttempt)).all()
    else:
        assert not posts(sandbox) and not session.scalars(select(OpsQueueItem)).all()
    assert [r.method for r in sandbox.calls][0] == "GET"
    event = session.scalars(select(TraceEvent).where(TraceEvent.name == "application_payment_resolution")).one()
    assert event.output["action"] == action.value


def test_repeat_different_dispute_creates_approval_never_second_post(financial_case, session_factory, session, sandbox):
    first = financial_case(1, "D9-1")
    second = financial_case(199, "D9-2")
    payment = service(session_factory, sandbox)
    assert payment.process_dispute("R003", first)["action"] == "AUTO_PAY"
    result = payment.process_dispute("R003", second)
    assert result["action"] == "OPS_APPROVAL" and result["amount"] == 199
    assert len(posts(sandbox)) == 1
    assert session.scalars(select(OpsQueueItem)).one().amount == 199


def test_same_dispute_reconciles_to_no_payment_and_never_reposts(financial_case, session_factory, sandbox):
    identifier = financial_case(25)
    payment = service(session_factory, sandbox)
    first = payment.process_dispute("R003", identifier)
    restarted = service(session_factory, sandbox).process_dispute("R003", identifier)
    assert restarted["action"] == "NO_PAYMENT" and restarted["amount"] == 0
    assert restarted["payment"]["payout_id"] == first["payment"]["payout_id"]
    assert len(posts(sandbox)) == 1
    assert len([r for r in sandbox.calls if r.method == "GET"]) == 2


def test_local_processed_row_is_not_proof_of_payment(financial_case, session_factory, session, sandbox):
    identifier = financial_case(25)
    payment = service(session_factory, sandbox)
    payment.process_dispute("R003", identifier)
    sandbox.ledger.clear()  # Simulate a provider ledger reset/disagreement.
    result = service(session_factory, sandbox).process_dispute("R003", identifier)
    assert result["action"] == "ESCALATION" and result["amount"] == 0
    session.expire_all()
    assert session.scalars(select(PaymentAttempt)).one().status == "unknown"
    assert len(posts(sandbox)) == 1


def test_existing_approval_is_not_executed_even_after_amount_drops(financial_case, session_factory, session, sandbox):
    identifier = financial_case(201)
    payment = service(session_factory, sandbox)
    first = payment.process_dispute("R003", identifier)
    session.get(PayoutLine, "L-D9").amount = 553 - 199
    session.commit()
    next_day = service(session_factory, sandbox, NOW + timedelta(days=1)).process_dispute("R003", identifier)
    assert first["action"] == next_day["action"] == "OPS_APPROVAL"
    assert next_day["amount"] == 199 and not posts(sandbox)
    session.expire_all()
    assert session.scalars(select(OpsQueueItem)).one().amount == 199


def test_unknown_post_outcome_is_held_until_provider_reconciliation(financial_case, session_factory, session, sandbox):
    identifier = financial_case(25)
    sandbox.lost_response = True
    first = service(session_factory, sandbox).process_dispute("R003", identifier)
    assert first["action"] == "ESCALATION" and first["amount"] == 0
    assert len(posts(sandbox)) == 1
    second = service(session_factory, sandbox).process_dispute("R003", identifier)
    assert second["action"] == "NO_PAYMENT" and second["payment"]["payout_id"] == "P1"
    assert len(posts(sandbox)) == 1
    session.expire_all()
    assert session.scalars(select(OpsQueueItem)).one().status == "resolved"


def test_money_moving_service_accepts_neither_amount_nor_model_authorization(financial_case, session_factory, sandbox):
    identifier = financial_case(25)
    payment = service(session_factory, sandbox)
    with pytest.raises(TypeError):
        payment.process_dispute("R003", identifier, amount=999)
    with pytest.raises(TypeError):
        payment.process_dispute("R003", identifier, authorization="AUTO_PAY")
    assert not posts(sandbox)
