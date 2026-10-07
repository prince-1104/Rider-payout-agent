"""SQLAlchemy engine, session factory and the FastAPI session dependency."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def build_engine(database_url: str) -> Engine:
    """Create an engine with sensible pooling defaults for the given URL."""
    if database_url.startswith("sqlite"):
        # SQLite is only used for local tooling/tests; allow cross-thread use by the test client.
        return create_engine(database_url, connect_args={"check_same_thread": False})
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine, created lazily so importing the app never opens a connection."""
    return build_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    Services own their commits. Any exception escaping the request rolls back
    uncommitted work; the session is always closed.
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
