"""Money-moving application boundary: fresh evidence, provider reconciliation and no blind retries."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.db.models import PaymentAttempt, StoredDispute, TraceEvent, PayoutLine
from app.domain.dispute import DisputeRequest
from app.domain.dispute_types import DisputeType
from app.integrations.payswift import PaySwiftClient, PaySwiftProtocolError
from app.services.conversation_service import ConversationService
from app.services.dispute_service import DisputeService
from app.services.payment_service import (
    PaymentOutcomeUnknown, PaymentPolicyDenied, PaymentService,
    auto_payment_eligibility, list_rider_payments, scopes_overlap,
)
from tests.test_agent_tools import NOW, seeded  # Shared CSV-derived financial fixture.


@pytest.fixture
def sandbox(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("PAYSWIFT_BASE_URL", "http://payswift.test")
    get_settings.cache_clear()
    remote = SimpleNamespace(calls=[], ledger=[], lost_response=False, failure=None, before_post=None)

    def handler(request):
        remote.calls.append(request)
        if request.method == "GET":
            assert request.url.params["rider_id"] in {"R003", "R005", "R016", "R033"}
            return httpx.Response(200, json={"payouts": remote.ledger, "count": len(remote.ledger)})
        if remote.before_post:
            remote.before_post(request)
        if remote.failure:
            return httpx.Response(remote.failure, json={"error": "provider-failure"})
        body = json.loads(request.content)
        assert request.headers["Idempotency-Key"]
        payment = {**body, "payout_id": f"P{len(remote.ledger)+1}", "status": "processed", "created_at": NOW.isoformat()}
        remote.ledger.append(payment)
        if remote.lost_response:
            raise httpx.ReadTimeout("response lost after payment", request=request)
        return httpx.Response(201, json=payment)

    remote.factory = lambda: PaySwiftClient(transport=httpx.MockTransport(handler))
    yield remote
    get_settings.cache_clear()


def payment_service(session_factory, sandbox, now=NOW):
    return PaymentService(session_factory, payswift_factory=sandbox.factory, clock=lambda: now)


def posts(sandbox):
    return [request for request in sandbox.calls if request.method == "POST"]


def test_application_recalculates_shortfall_and_persists_before_http(seeded, session_factory, sandbox):
    def verify_reserved(request):
        with session_factory() as session:
            attempt = session.scalars(select(PaymentAttempt)).one()
            assert attempt.status == "reserved" and attempt.amount == 25
            assert request.headers["Idempotency-Key"] == attempt.idempotency_key
            assert json.loads(request.content)["reference"] == attempt.reference
    sandbox.before_post = verify_reserved
    service = payment_service(session_factory, sandbox)
    result = service.pay_dispute("R003", "D3")
    assert result["amount"] == 25 and result["status"] == "processed"
    assert len(posts(sandbox)) == 1
    repeated = service.pay_dispute("R003", "D3")
    assert repeated == result and len(posts(sandbox)) == 1
    with session_factory() as session:
        assert session.scalars(select(TraceEvent).where(TraceEvent.type == "decision")).all()


def test_claim_amount_and_stored_owed_amount_do_not_determine_payment(seeded, session_factory, session, sandbox):
    dispute = session.get(StoredDispute, "D3")
    dispute.claim = {**dispute.claim, "claimed_amount": 5000}
    dispute.claimed_amount = 5000
    # Even a stale yet internally consistent persisted calculation is not authority.
    dispute.expected_amount, dispute.actual_amount, dispute.amount_owed = 200, 0, 200
    session.commit()
    result = payment_service(session_factory, sandbox).pay_dispute("R003", "D3")
    assert result["amount"] == 25
    assert json.loads(posts(sandbox)[0].content)["amount"] == 25


def test_application_normalizes_trusted_identity_before_payment_and_trace(seeded, session_factory, session, sandbox):
    result = payment_service(session_factory, sandbox).pay_dispute(" r3 ", "D3")
    assert result["status"] == "processed" and result["amount"] == 25
    assert json.loads(posts(sandbox)[0].content)["rider_id"] == "R003"
    event = session.scalars(select(TraceEvent).where(TraceEvent.type == "decision")).one()
    assert event.rider_id == "R003"


def test_changed_internal_ledger_prevents_payment(seeded, session_factory, session, sandbox):
    session.add(PayoutLine(line_id="CORRECTION", rider_id="R003", payout_date=date(2026, 9, 20),
                           line_type="trip", trip_id="T926334", amount=25))
    session.commit()
    with pytest.raises(PaymentPolicyDenied):
        payment_service(session_factory, sandbox).pay_dispute("R003", "D3")
    assert not posts(sandbox)


@pytest.mark.parametrize("rider,dispute", [("R005", "D3"), ("R003", "D5"), ("R003", "MISSING")])
def test_payment_rejects_foreign_or_unknown_disputes(seeded, session_factory, sandbox, rider, dispute):
    with pytest.raises(PaymentPolicyDenied):
        payment_service(session_factory, sandbox).pay_dispute(rider, dispute)
    assert not posts(sandbox)


def test_large_dispute_requires_approval_and_cannot_be_split(seeded, session_factory, sandbox):
    with pytest.raises(PaymentPolicyDenied, match="₹200"):
        payment_service(session_factory, sandbox).pay_dispute("R016", "D16")
    assert not posts(sandbox)


def test_expired_verified_dispute_is_rejected_on_fresh_recheck(seeded, session_factory, sandbox):
    with pytest.raises(PaymentPolicyDenied):
        payment_service(session_factory, sandbox, now=NOW + timedelta(days=20)).pay_dispute("R003", "D3")
    assert not posts(sandbox)


@pytest.mark.parametrize("status", ["needs_information", "needs_review", "rejected"])
def test_unverified_dispute_does_not_move_money(seeded, session_factory, session, sandbox, status):
    dispute = session.get(StoredDispute, "D3")
    dispute.status, dispute.amount_owed = status, 0
    session.commit()
    with pytest.raises(PaymentPolicyDenied):
        payment_service(session_factory, sandbox).pay_dispute("R003", "D3")
    assert not posts(sandbox)


def test_existing_provider_payment_today_prevents_second_auto_payment(seeded, session_factory, sandbox):
    sandbox.ledger.append({"payout_id": "OLD", "rider_id": "R003", "amount": 1,
        "status": "processed", "created_at": NOW.isoformat(), "reference": "external"})
    with pytest.raises(PaymentPolicyDenied, match="already received"):
        payment_service(session_factory, sandbox).pay_dispute("R003", "D3")
    assert not posts(sandbox)


def test_unattributed_historical_provider_credit_fails_closed(seeded, session_factory, sandbox):
    sandbox.ledger.append({"payout_id": "OLD", "rider_id": "R003", "amount": 25,
        "status": "processed", "created_at": (NOW-timedelta(days=1)).isoformat(), "reference": "unknown"})
    with pytest.raises(PaymentPolicyDenied, match="unattributed"):
        payment_service(session_factory, sandbox).pay_dispute("R003", "D3")
    assert not posts(sandbox)


def test_lost_response_reconciles_provider_reference_without_second_post(seeded, session_factory, sandbox):
    sandbox.lost_response = True
    service = payment_service(session_factory, sandbox)
    with pytest.raises(PaymentOutcomeUnknown):
        service.pay_dispute("R003", "D3")
    assert len(posts(sandbox)) == 1
    with session_factory() as session:
        assert session.scalars(select(PaymentAttempt.status)).one() == "unknown"
    # A different service object represents a restart; reference/key came from the database.
    result = payment_service(session_factory, sandbox).pay_dispute("R003", "D3")
    assert result["status"] == "processed" and result["payout_id"] == "P1"
    assert len(posts(sandbox)) == 1


@pytest.mark.parametrize("failure", [400, 409, 429, 500, 503])
def test_failed_post_is_held_for_reconciliation_and_not_retried(seeded, session_factory, sandbox, failure):
    sandbox.failure = failure
    service = payment_service(session_factory, sandbox)
    with pytest.raises(PaymentOutcomeUnknown):
        service.pay_dispute("R003", "D3")
    assert len(posts(sandbox)) == 1
    assert service.pay_dispute("R003", "D3")["status"] == "unknown"
    assert len(posts(sandbox)) == 1


def test_overlapping_duplicate_claim_cannot_be_paid_on_next_day(seeded, session_factory, session, sandbox):
    payment_service(session_factory, sandbox).pay_dispute("R003", "D3")
    service = ConversationService(session)
    cid = session.scalar(select(StoredDispute.conversation_id).where(StoredDispute.dispute_id == "D3"))
    claim = DisputeRequest(dispute_id="D3-duplicate", dispute_type=DisputeType.MISSING_SURGE, trip_id="T926334")
    service.create_dispute("R003", conversation_id=cid, claim=claim)
    result = DisputeService(session).evaluate("R003", claim, reference_date=NOW)
    service.record_dispute_result("R003", conversation_id=cid, result=result, expected_revision=1)
    session.commit()
    with pytest.raises(PaymentPolicyDenied, match="overlaps"):
        payment_service(session_factory, sandbox, now=NOW+timedelta(days=1)).pay_dispute("R003", "D3-duplicate")
    assert len(posts(sandbox)) == 1


@pytest.mark.parametrize("record", [
    {"payout_id": "P", "rider_id": "R005", "amount": 25, "status": "processed", "created_at": NOW.isoformat()},
    {"payout_id": "P", "rider_id": "R003", "amount": -1, "status": "processed", "created_at": NOW.isoformat()},
    {"payout_id": "P", "rider_id": "R003", "amount": 25, "status": "processed", "created_at": "2026-09-22T10:00:00"},
])
def test_untrustworthy_provider_records_prevent_payment(seeded, session_factory, sandbox, record):
    sandbox.ledger.append(record)
    with pytest.raises(PaySwiftProtocolError):
        payment_service(session_factory, sandbox).pay_dispute("R003", "D3")
    assert not posts(sandbox)


@pytest.mark.parametrize("payload", [{}, {"count": 1, "payouts": []}, {"payouts": "bad"}])
def test_incomplete_provider_ledger_is_not_assumed_empty(payload):
    client = SimpleNamespace(list_payouts=lambda rider_id: payload)
    with pytest.raises(PaySwiftProtocolError):
        list_rider_payments(client, "R003")


def test_unknown_prior_attempt_blocks_payment_even_on_later_day():
    result = auto_payment_eligibility(25, [], [SimpleNamespace(status="unknown", payment_date=date(2026, 9, 21))], NOW)
    assert not result["eligible"]


def test_pending_provider_payment_is_not_treated_as_zero_paid():
    result = auto_payment_eligibility(25, [SimpleNamespace(status="pending", created_at=NOW)], [], NOW)
    assert not result["eligible"]


@pytest.mark.parametrize("left,right,overlaps", [
    ({"date": "2026-09-20", "trip_id": "T1", "type": "missing_surge"}, {"date": "2026-09-20", "trip_id": "T1", "type": "missing_trip_payout"}, True),
    ({"date": "2026-09-20", "trip_id": None, "type": "general_payout_dispute"}, {"date": "2026-09-20", "trip_id": "T1", "type": "missing_surge"}, True),
    ({"date": "2026-09-20", "trip_id": "T1", "type": "missing_surge"}, {"date": "2026-09-20", "trip_id": "T2", "type": "missing_surge"}, False),
    ({"date": "2026-09-20", "trip_id": None, "type": "missing_daily_incentive"}, {"date": "2026-09-20", "trip_id": "T1", "type": "missing_surge"}, False),
])
def test_scope_overlap_prevents_repeat_or_combined_claim_overpayment(left, right, overlaps):
    assert scopes_overlap(left, right) is overlaps
