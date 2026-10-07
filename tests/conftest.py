"""Shared pytest fixtures.

Tests run against in-memory SQLite by default so they need no external services.
Set TEST_DATABASE_URL to a PostgreSQL URL to run the same suite against Postgres
(tables are created and dropped around every test).
"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import models  # noqa: F401  (registers models on Base.metadata)
from app.db.database import Base, get_session
from app.main import create_app


def _test_database_url() -> str:
    raw = os.environ.get("TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    # Reuse the production normaliser so postgresql:// URLs map to psycopg.
    return Settings(database_url=raw).database_url


@pytest.fixture()
def engine() -> Iterator[Engine]:
    url = _test_database_url()
    if url.startswith("sqlite"):
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,  # one shared connection so :memory: survives across sessions
        )
    else:
        eng = create_engine(url, pool_pre_ping=True)

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = create_app()

    def _override_get_session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override_get_session
    with TestClient(app) as test_client:
        yield test_client
