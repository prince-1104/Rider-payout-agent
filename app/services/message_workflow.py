"""Durable webhook ownership and recovery, independent of any model decision.

Short committed leases serialize a rider's conversation across application
workers. Never hold a database transaction open across model/provider requests.
An unfinished delivery receives a retryable status, not a fabricated final reply.
"""

from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

import httpx
from sqlalchemy import or_, select, update

from app.agent.model import UnavailableModel, configured_model
from app.agent.orchestrator import Orchestrator
from app.agent.state import AgentState
from app.agent.tools import ToolContext
from app.db.models import Conversation, InboundMessage
from app.integrations.payswift import PaySwiftClient
from app.services.conversation_service import ConversationService
from app.services.idempotency import register_inbound_message
from app.services.trace import TraceRecorder


class MessageInProgress(RuntimeError):
    """Vendor should retry; no final reply is available yet."""


class WorkflowDeadline(RuntimeError):
    """Stop investigation/financial work and persist a safe escalation."""


def utc_now():
    return datetime.now(timezone.utc)


def webhook_payswift():
    return PaySwiftClient(timeout=httpx.Timeout(connect=0.5, read=1, write=1, pool=0.5))


class MessageWorkflow:
    LEASE_SECONDS = 120
    BUDGET_SECONDS = 8

    def __init__(self, session_factory, *, model_factory=configured_model,
                 payswift_factory=webhook_payswift, clock=utc_now):
        self.sessions = session_factory
        self.model_factory = model_factory
        self.payswift_factory = payswift_factory
        self.clock = clock

    def prepare(self, payload):
        """Persist the original delivery before executing any financial workflow."""
        with self.sessions() as session:
            registration = register_inbound_message(session, payload)
            message = registration.message
            session.commit()
            if message.reply is not None:
                return message.reply, None
            # A retry always uses original persisted identity, text and timestamp.
            original = payload.model_validate({"message_id": message.message_id, "rider_id": message.rider_id,
                "text": message.text, "received_at": self._aware(message.received_at)})
            conversation = ConversationService(session).get_or_create_conversation(message.rider_id)
            conversation_id = conversation.conversation_id
            session.commit()
        return None, (original, conversation_id)

    @staticmethod
    def _aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    def _claim(self, payload, conversation_id, token):
        now = self.clock()
        expires = now + timedelta(seconds=self.LEASE_SECONDS)
        with self.sessions() as session:
            # Same lock order for every worker: conversation then inbound message.
            claimed = session.execute(update(Conversation).where(
                Conversation.conversation_id == conversation_id, Conversation.rider_id == payload.rider_id,
                or_(Conversation.workflow_token.is_(None), Conversation.workflow_expires_at <= now),
            ).values(workflow_token=token, workflow_expires_at=expires)).rowcount
            if claimed != 1:
                session.rollback()
                raise MessageInProgress("Conversation is being processed")
            claimed = session.execute(update(InboundMessage).where(
                InboundMessage.message_id == payload.message_id, InboundMessage.rider_id == payload.rider_id,
                InboundMessage.reply.is_(None),
                or_(InboundMessage.processing_token.is_(None), InboundMessage.processing_expires_at <= now),
            ).values(processing_token=token, processing_expires_at=expires)).rowcount
            if claimed != 1:
                session.rollback()
                raise MessageInProgress("Message is being processed or has just completed")
            session.commit()

    def _guard(self, payload, conversation_id, token):
        now = self.clock()
        with self.sessions() as session:
            owned = session.scalar(select(InboundMessage.id).join(Conversation,
                Conversation.conversation_id == conversation_id).where(
                InboundMessage.message_id == payload.message_id, InboundMessage.rider_id == payload.rider_id,
                InboundMessage.reply.is_(None), InboundMessage.processing_token == token,
                InboundMessage.processing_expires_at > now, Conversation.rider_id == payload.rider_id,
                Conversation.workflow_token == token, Conversation.workflow_expires_at > now))
            if owned is None:
                raise MessageInProgress("Workflow ownership expired")

    def _release(self, payload, conversation_id, token):
        with self.sessions() as session:
            session.execute(update(Conversation).where(Conversation.conversation_id == conversation_id,
                Conversation.workflow_token == token).values(workflow_token=None, workflow_expires_at=None))
            session.execute(update(InboundMessage).where(InboundMessage.message_id == payload.message_id,
                InboundMessage.processing_token == token).values(processing_token=None, processing_expires_at=None))
            session.commit()

    def execute(self, prepared, *, cache_reply):
        payload, conversation_id = prepared
        token = str(uuid4())
        self._claim(payload, conversation_id, token)
        started = monotonic()
        recorder = TraceRecorder(self.sessions, payload.rider_id)
        try:
            invocation = recorder.start("message_workflow", {"message_id": payload.message_id,
                "conversation_id": conversation_id}, kind="message_in")

            def financial_guard():
                self._guard(payload, conversation_id, token)
                if monotonic() - started >= self.BUDGET_SECONDS:
                    raise WorkflowDeadline("Webhook investigation budget exhausted")

            def step_guard(name):
                self._guard(payload, conversation_id, token)
                if name not in {"load_conversation", "application_escalation", "final_response"}:
                    financial_guard()

            try:
                model = self.model_factory()
            except Exception:
                model = UnavailableModel()
            context = ToolContext(payload.rider_id, self.sessions, self.clock(), self.payswift_factory)
            state = Orchestrator(context, model, step_guard=step_guard, payment_guard=financial_guard).run(
                conversation_id=conversation_id, message_id=payload.message_id,
                latest_message=payload.text, received_at=payload.received_at)
            state = AgentState.model_validate(state.model_dump())
            # Assistant turn/state committed by the orchestrator before caching.
            # If caching fails, a retry replays that turn without redoing money.
            self._guard(payload, conversation_id, token)
            with self.sessions() as session:
                message = session.scalar(select(InboundMessage).where(
                    InboundMessage.message_id == payload.message_id,
                    InboundMessage.processing_token == token).with_for_update())
                if message is None:
                    raise MessageInProgress("Workflow ownership changed")
                if message.workflow_state is None:
                    message.workflow_state = state.model_dump(mode="json")
                    session.commit()
                cache_reply(session, message, state.reply)
            recorder.finish(invocation, output={"reply": state.reply, "agent_iterations": state.agent_iterations,
                "actions": [d.action.value for d in state.policy_decision]})
            return state.reply
        except Exception as exc:
            if "invocation" in locals():
                recorder.finish(invocation, error=exc)
            raise
        finally:
            self._release(payload, conversation_id, token)
