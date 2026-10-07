"""ORM models."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

# BIGINT identity on Postgres; SQLite only auto-increments INTEGER PRIMARY KEY.
_BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class InboundMessage(Base):
    """A rider message as delivered by the messaging vendor.

    ``message_id`` is the vendor's wamid. The vendor retries when it does not get
    a 2xx within ~10s, so the same wamid may arrive several times; the UNIQUE
    constraint is the last line of defence against processing it twice.
    """

    __tablename__ = "inbound_messages"
    __table_args__ = (UniqueConstraint("message_id", name="uq_inbound_messages_message_id"),)

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rider_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"InboundMessage(id={self.id!r}, message_id={self.message_id!r}, rider_id={self.rider_id!r})"
