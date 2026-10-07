"""Migration parity and concurrent safety of the application payment reservation."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import Base
from app.db.models import PaymentAttempt, PayoutLine, Rider, TraceEvent, Trip
from app.domain.dispute import DisputeRequest
from app.domain.dispute_types import DisputeType
from app.integrations.payswift import PaySwiftClient
from app.services.conversation_service import ConversationService
from app.services.dispute_service import DisputeService
from app.services.payment_service import PaymentPolicyDenied, PaymentService

NOW = datetime(2026, 9, 22, 4, 30, tzinfo=timezone.utc)


def test_phase6_migration_matches_models_and_downgrades(tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{(tmp_path / 'tools.db').as_posix()}")
    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, "0003_conversation_state")
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_concurrent_payments_cannot_reserve_more_than_one_rider_day(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("PAYSWIFT_BASE_URL", "http://payswift.test")
    get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{(tmp_path / 'payments.db').as_posix()}",
                           connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Rider(rider_id="R003", name="Test", city="Test", joined_on=date(2026, 1, 1)))
        session.flush()
        session.add(Trip(trip_id="T926334", rider_id="R003", started_at=NOW.replace(day=20),
                         status="completed", distance_km=6, surge_multiplier=1.5))
        session.flush()
        session.add(PayoutLine(line_id="P1", rider_id="R003", payout_date=date(2026, 9, 20),
                               trip_id="T926334", line_type="trip", amount=49))
        session.flush()
        conversations = ConversationService(session)
        conversation = conversations.get_or_create_conversation("R003")
        for identifier in ["D1", "D2"]:
            claim = DisputeRequest(dispute_id=identifier, dispute_type=DisputeType.MISSING_SURGE, trip_id="T926334")
            conversations.create_dispute("R003", conversation_id=conversation.conversation_id, claim=claim)
            result = DisputeService(session).evaluate("R003", claim, reference_date=NOW)
            conversations.record_dispute_result("R003", conversation_id=conversation.conversation_id, result=result, expected_revision=1)
        session.commit()
    calls, ledger = [], []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"payouts": list(ledger), "count": len(ledger)})
        calls.append(request)
        body = json.loads(request.content)
        payment = {**body, "payout_id": "P-NEW", "status": "processed", "created_at": NOW.isoformat()}
        ledger.append(payment)
        return httpx.Response(201, json=payment)

    def attempt(identifier):
        service = PaymentService(factory, payswift_factory=lambda: PaySwiftClient(transport=httpx.MockTransport(handler)), clock=lambda: NOW)
        try:
            return service.pay_dispute("R003", identifier)
        except PaymentPolicyDenied:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ["D1", "D2"]))
        assert len([result for result in results if result is not None]) == 1
        assert len(calls) == 1
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(PaymentAttempt)) == 1
            assert session.scalar(select(func.count()).select_from(TraceEvent).where(TraceEvent.type == "decision")) == 2
    finally:
        engine.dispose()
        get_settings.cache_clear()
