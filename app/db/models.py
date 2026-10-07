"""ORM models."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
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
    processing_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    processing_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    workflow_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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


class Conversation(Base):
    """One durable conversation per vendor-authenticated rider, even before CSV import."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("rider_id", name="uq_conversations_rider_id"),
        UniqueConstraint("conversation_id", "rider_id", name="uq_conversations_identity"),
        CheckConstraint("message_count >= 0 AND version >= 0", name="ck_conversations_counters"),
    )

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    rider_id: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workflow_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workflow_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ConversationMessage(Base):
    """Ordered turns; rider and assistant turns share the triggering vendor ID."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id", "rider_id"],
                             ["conversations.conversation_id", "conversations.rider_id"],
                             ondelete="CASCADE", name="fk_conversation_messages_identity"),
        UniqueConstraint("message_id", "role", name="uq_conversation_messages_vendor_role"),
        UniqueConstraint("conversation_id", "sequence", name="uq_conversation_messages_sequence"),
        CheckConstraint("role IN ('rider', 'assistant')", name="ck_conversation_messages_role"),
        CheckConstraint("sequence > 0", name="ck_conversation_messages_sequence"),
    )

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    rider_id: Mapped[str] = mapped_column(String(32), nullable=False)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class StoredDispute(Base):
    """Persistent claim and calculation, scoped by conversation and authenticated identity."""

    __tablename__ = "disputes"
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id", "rider_id"],
                             ["conversations.conversation_id", "conversations.rider_id"],
                             ondelete="CASCADE", name="fk_disputes_identity"),
        CheckConstraint("amount_owed >= 0", name="ck_disputes_nonnegative"),
        CheckConstraint("amount_owed = 0 OR (status = 'valid' AND expected_amount IS NOT NULL "
                        "AND actual_amount IS NOT NULL AND amount_owed <= expected_amount - actual_amount)",
                        name="ck_disputes_verified_shortfall"),
    )

    dispute_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    rider_id: Mapped[str] = mapped_column(String(32), nullable=False)
    dispute_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trip_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispute_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    claimed_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_owed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="needs_information")
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    claim: Mapped[dict] = mapped_column(JSON, nullable=False)
    revisions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TraceEvent(Base):
    """Durable tool invocation record, including validation and execution failures."""

    __tablename__ = "trace_events"
    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    invocation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    rider_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)


class PaymentAttempt(Base):
    """A durable reservation; uniqueness also blocks concurrent auto-payment requests."""

    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint("dispute_id", name="uq_payment_attempts_dispute"),
        Index("uq_payment_attempts_rider_day_auto", "rider_id", "payment_date", unique=True,
              sqlite_where=text("ops_approved = 0"), postgresql_where=text("ops_approved = false")),
        UniqueConstraint("idempotency_key", name="uq_payment_attempts_key"),
        UniqueConstraint("reference", name="uq_payment_attempts_reference"),
        CheckConstraint("amount > 0", name="ck_payment_attempts_amount"),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(String(128), ForeignKey("disputes.dispute_id"), nullable=False)
    rider_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[dict] = mapped_column(JSON, nullable=False)
    reference: Mapped[str] = mapped_column(String(256), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payout_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ops_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OpsQueueItem(Base):
    """Pending application-created approval/escalation; models cannot insert items."""

    __tablename__ = "ops_queue_items"
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id", "rider_id"],
                             ["conversations.conversation_id", "conversations.rider_id"],
                             name="fk_ops_queue_identity"),
        CheckConstraint("(type = 'approval' AND amount IS NOT NULL AND amount > 0) OR "
                        "(type = 'escalation' AND amount IS NULL)", name="ck_ops_queue_amount"),
        CheckConstraint("status IN ('pending', 'resolved', 'rejected')", name="ck_ops_queue_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rider_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dispute_id: Mapped[str | None] = mapped_column(String(128), ForeignKey("disputes.dispute_id"), nullable=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
