"""The UNIQUE constraint on inbound_messages.message_id, at the ORM, service and migration level."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import InboundMessage
from app.db.repositories import InboundMessageRepository
from app.schemas.messages import InboundMessageIn
from app.services import idempotency

IST = timezone(timedelta(hours=5, minutes=30))
RECEIVED_AT = datetime(2026, 9, 22, 9, 5, tzinfo=IST)
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_duplicate_message_id_violates_unique_constraint(session: Session) -> None:
    repo = InboundMessageRepository(session)
    repo.add(message_id="wamid.DUP", rider_id="R003", text="first", received_at=RECEIVED_AT)
    session.commit()

    with pytest.raises(IntegrityError):
        repo.add(message_id="wamid.DUP", rider_id="R003", text="second", received_at=RECEIVED_AT)
    session.rollback()

    assert session.scalar(select(func.count()).select_from(InboundMessage)) == 1


def test_unique_constraint_is_named_on_model() -> None:
    names = {c.name for c in InboundMessage.__table__.constraints}
    assert "uq_inbound_messages_message_id" in names


def test_register_recovers_when_losing_insert_race(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a concurrent delivery that inserts the same wamid after our pre-check."""
    other = InboundMessageRepository(session)
    other.add(message_id="wamid.RACE", rider_id="R003", text="t", received_at=RECEIVED_AT)
    other.set_reply(session.scalars(select(InboundMessage)).one(), "stored reply")
    session.commit()

    real_get = InboundMessageRepository.get_by_message_id
    calls = {"n": 0}

    def get_missing_first(self: InboundMessageRepository, message_id: str) -> InboundMessage | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else real_get(self, message_id)

    monkeypatch.setattr(InboundMessageRepository, "get_by_message_id", get_missing_first)

    payload = InboundMessageIn(
        message_id="wamid.RACE", rider_id="R003", text="t", received_at=RECEIVED_AT
    )
    result = idempotency.register_inbound_message(session, payload)

    assert result.is_duplicate is True
    assert result.message.reply == "stored reply"
    assert session.scalar(select(func.count()).select_from(InboundMessage)) == 1


def test_migration_creates_unique_constraint(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "head")

    engine = create_engine(url)
    insert = text(
        "INSERT INTO inbound_messages (message_id, rider_id, text, received_at) "
        "VALUES (:m, 'R003', 'hi', '2026-09-22T09:05:00+05:30')"
    )
    try:
        with engine.begin() as conn:
            conn.execute(insert, {"m": "wamid.MIG"})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"m": "wamid.MIG"})
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
