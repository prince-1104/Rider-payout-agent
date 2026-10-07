"""ORM models."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


class Rider(Base):
    """Registered QuickDrop rider profile."""

    __tablename__ = "riders"

    rider_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    city: Mapped[str] = mapped_column(String(64), nullable=False)
    joined_on: Mapped[date] = mapped_column(Date, nullable=False)

    trips: Mapped[list["Trip"]] = relationship(
        back_populates="rider", cascade="all, delete-orphan"
    )
    payout_lines: Mapped[list["PayoutLine"]] = relationship(
        back_populates="rider", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"Rider(rider_id={self.rider_id!r}, name={self.name!r}, city={self.city!r})"


class Trip(Base):
    """Dispatch system trip record."""

    __tablename__ = "trips"

    trip_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rider_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("riders.rider_id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # 'completed', 'cancelled_by_customer', 'cancelled_by_rider'
    distance_km: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    surge_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("1.0")
    )

    rider: Mapped["Rider"] = relationship(back_populates="trips")
    payout_lines: Mapped[list["PayoutLine"]] = relationship(back_populates="trip")

    def __repr__(self) -> str:
        return (
            f"Trip(trip_id={self.trip_id!r}, rider_id={self.rider_id!r}, "
            f"status={self.status!r}, dist={self.distance_km}, surge={self.surge_multiplier})"
        )


class PayoutLine(Base):
    """Financial payout ledger item as disbursed or deducted."""

    __tablename__ = "payout_lines"

    line_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payout_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    rider_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("riders.rider_id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # 'trip', 'daily_incentive', 'cancellation_penalty'
    trip_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("trips.trip_id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # Integer whole rupees (penalties are negative)

    rider: Mapped["Rider"] = relationship(back_populates="payout_lines")
    trip: Mapped["Trip | None"] = relationship(back_populates="payout_lines")

    def __repr__(self) -> str:
        return (
            f"PayoutLine(line_id={self.line_id!r}, date={self.payout_date}, "
            f"rider_id={self.rider_id!r}, type={self.line_type!r}, amt={self.amount})"
        )
