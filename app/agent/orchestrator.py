"""Fixed, bounded three-agent workflow; all writes belong to the application.

No autonomous executor, dynamic tool names, model-chosen amounts or free-text
financial parsing. Construct one orchestrator with trusted context per request.
"""

import re
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from app.agent.agents.conversation_agent import ConversationAgent
from app.agent.agents.investigation_agent import InvestigationAgent
from app.agent.agents.resolution_agent import ResolutionAgent
from app.agent.policy import decide
from app.agent.security import PromptInjectionDetected, claim_data, classify_input, redact_secrets
from app.agent.state import Action, AgentState, HistoryTurn, PolicyDecision
from app.agent.tools import ToolContext, build_toolset
from app.db.models import ConversationMessage, PaymentAttempt, StoredDispute
from app.domain.dispute import DisputeRequest
from app.domain.dispute_types import DisputeStatus
from app.domain.policy import to_ist_date
from app.schemas.conversations import ConversationState
from app.schemas.trace import TraceStep
from app.services.conversation_service import ConversationAccessError, ConversationService
from app.services.dispute_service import DisputeService
from app.services.ops_service import OpsService
from app.services.payment_service import PaymentService, traced_list_rider_payments
from app.services.trace import TraceRecorder


class AgentIterationLimit(RuntimeError):
    """Application budget exhausted; never ask a model whether to keep running."""


class Orchestrator:
    MAX_AGENT_ITERATIONS = 5

    def __init__(self, context: ToolContext, model, *, max_iterations=5, step_guard=None, payment_guard=None):
        if type(max_iterations) is not int or not 1 <= max_iterations <= self.MAX_AGENT_ITERATIONS:
            raise ValueError("max_iterations must be between 1 and 5")
        self.context = context
        self.max_iterations = max_iterations
        self.step_guard = step_guard
        self.conversation_agent = ConversationAgent(model)
        self.investigation_agent = InvestigationAgent(model)
        self.resolution_agent = ResolutionAgent(model)
        self.tools = {t.name: t for t in build_toolset(context).read_tools}
        self.recorder = TraceRecorder(context.session_factory, context.rider_id)
        self.payments = PaymentService(context.session_factory, payswift_factory=context.payswift_factory,
                                       clock=lambda: context.now, execution_guard=payment_guard)

    def _step(self, state, name, inputs, function, *, agent=False, kind="decision", trace_output=None):
        inputs = redact_secrets(inputs)
        identifier = self.recorder.start(name, inputs, kind=kind)
        at = datetime.now(timezone.utc)
        try:
            if self.step_guard:
                self.step_guard(name)
            if agent:
                if state.agent_iterations >= self.max_iterations:
                    raise AgentIterationLimit("Agent iteration limit reached")
                state.agent_iterations += 1
            output = function()
        except Exception as exc:
            self.recorder.finish(identifier, error=exc)
            state.trace.append(TraceStep(at=at, type="error", name=name,
                                         input=inputs, output={"error": type(exc).__name__}))
            raise
        encoded = jsonable_encoder(output if trace_output is None else trace_output(output))
        self.recorder.finish(identifier, output=encoded)
        state.trace.append(TraceStep(at=at, type=kind, name=name, input=inputs, output=encoded))
        return output

    def run(self, *, conversation_id: str, message_id: str, latest_message: str,
            received_at: datetime | None = None) -> AgentState:
        if not isinstance(latest_message, str) or len(latest_message) > 4096:
            raise ValueError("latest_message must be text of at most 4096 characters")
        state = AgentState(rider_id=self.context.rider_id, conversation_id=conversation_id,
                           latest_message=latest_message)
        replay = self._step(state, "load_conversation", {"conversation_id": conversation_id, "message_id": message_id},
                            lambda: self._load(state, message_id, received_at), kind="message_in",
                            trace_output=lambda value: {"replayed": value is not None})
        if replay is not None:
            state.reply = replay
            return state
        try:
            classification = self._step(state, "classify_input", {"message_id": message_id},
                lambda: classify_input(latest_message, state.rider_id))
            if classification.blocked:
                raise PromptInjectionDetected(classification.flags)
            extracted = self._step(state, "conversation_agent", {"message_id": message_id,
                "character_count": len(latest_message), "history_count": len(state.conversation_history)},
                lambda: self.conversation_agent.invoke(self._conversation_payload(state)), agent=True)
            state.claims = self._step(state, "validate_claims", extracted.model_dump(mode="json"),
                lambda: self._validate_claims(state, extracted), trace_output=lambda claims: {
                    "count": len(claims), "claims": [{"dispute_id": c.dispute_id,
                        "dispute_type": c.dispute_type.value, "trip_id": c.trip_id,
                        "dispute_date": c.dispute_date.isoformat() if c.dispute_date else None} for c in claims]})
            state.investigation_results = self._step(state, "investigation_agent",
                {"claims": [claim_data(c) for c in state.claims]},
                lambda: self.investigation_agent.investigate(state.claims, self.tools, self.context.today), agent=True,
                trace_output=lambda results: [{"dispute_id": r.dispute_id,
                    "evidence_tools": sorted(r.evidence.keys())} for r in results])
            state.validated_disputes = self._step(state, "deterministic_dispute_engine", {},
                lambda: self._evaluate(state), trace_output=lambda results: [{
                    "dispute_id": d.dispute_id, "dispute_type": d.dispute_type.value,
                    "dispute_date": d.dispute_date.isoformat() if d.dispute_date else None,
                    "status": d.status.value, "expected_amount": d.expected_amount,
                    "actual_amount": d.actual_amount, "amount_owed": d.amount_owed,
                } for d in results])
            state.policy_decision = self._step(state, "deterministic_policy_engine", {},
                lambda: self._policy(state, extracted.needs_information))
            self._step(state, "resolution_agent", {"decision_count": len(state.policy_decision)},
                       lambda: self.resolution_agent.recommend(state.validated_disputes, state.policy_decision), agent=True,
                       trace_output=lambda _: {"recommendation": "received", "financial_authority": "deterministic_policy"})
            state.execution_results = self._step(state, "application_execute", {},
                lambda: self._execute(state, message_id), trace_output=lambda outcomes: [{
                    "dispute_id": item.get("dispute_id"), "action": item.get("action"),
                    "amount": item.get("payment", {}).get("amount", item.get("amount")),
                    "payment_status": item.get("payment", {}).get("status"),
                    "ops_type": item.get("type")} for item in outcomes])
        except Exception as exc:
            # Malformed/unsafe LLM outputs and failed evidence reads never reach a write/payment tool.
            reason = "The automated investigation could not be safely completed; ops review is required"
            if isinstance(exc, PromptInjectionDetected):
                state.security_flags = list(exc.flags)
                reason = ("I cannot follow requests to change instructions, identity, permissions or policy, "
                          "disclose private data, or execute internal tools. Ops review is required")
            state.policy_decision = [PolicyDecision(dispute_id=None, action=Action.ESCALATION,
                reason=reason)]
            state.execution_results = self._step(state, "application_escalation", {},
                                                  lambda: self._execute(state, message_id))
        state.reply = self._step(state, "final_response", {}, lambda: self._finish(state, message_id), kind="reply")
        return state

    def _load(self, state, message_id, received_at=None):
        with self.context.session_factory() as session:
            service = ConversationService(session)
            history = service.get_conversation_history(state.rider_id, conversation_id=state.conversation_id)
            prior = session.scalar(select(ConversationMessage).where(
                ConversationMessage.message_id == message_id, ConversationMessage.role == "rider"))
            if prior is not None and (prior.rider_id != state.rider_id or prior.conversation_id != state.conversation_id
                                      or prior.text != state.latest_message):
                raise ConversationAccessError("Message identity conflicts with authenticated conversation")
            state.conversation_history = [HistoryTurn(role=m.role, text=redact_secrets(m.text)) for m in history[-50:]
                                          if m.message_id != message_id and not classify_input(m.text, state.rider_id).blocked]
            answered = next((m for m in history if m.message_id == message_id and m.role == "assistant"), None)
            if answered is not None:
                return answered.text
            service.append_message(state.rider_id, conversation_id=state.conversation_id, message_id=message_id,
                                   text=state.latest_message, received_at=received_at or self.context.now)
            session.commit()
        return None

    def _conversation_payload(self, state):
        with self.context.session_factory() as session:
            active = ConversationService(session).get_active_disputes(state.rider_id, conversation_id=state.conversation_id)
            return {"latest_message": state.latest_message,
                    "conversation_history": [h.model_dump() for h in state.conversation_history],
                    "reference_ist_date": self.context.today.isoformat(),
                    "active_claims": [claim_data(d.claim) for d in active]}

    def _validate_claims(self, state, extracted):
        claims = []
        with self.context.session_factory() as session:
            service = ConversationService(session)
            active = {d.dispute_id: d for d in service.get_active_disputes(
                state.rider_id, conversation_id=state.conversation_id)}
            seen = set()
            revised = set()
            rider_text = "\n".join([state.latest_message] + [h.text for h in state.conversation_history if h.role == "rider"])
            for claim in extracted.claims:
                existing = active.get(claim.existing_dispute_id)
                if claim.existing_dispute_id is not None and existing is None:
                    raise ConversationAccessError("Only an owned active dispute can be clarified")
                if claim.trip_id:
                    if (existing is None or existing.trip_id != claim.trip_id) and not re.search(
                        rf"(?<!\w){re.escape(claim.trip_id)}(?!\w)", rider_text, re.IGNORECASE,
                    ):
                        raise ValueError("Trip reference was not supplied by the rider")
                    trip = self.context.own_trip(session, claim.trip_id)
                    recorded_date = to_ist_date(trip.started_at)
                else:
                    recorded_date = None
                request = DisputeRequest(dispute_type=claim.dispute_type, trip_id=claim.trip_id,
                    dispute_date=claim.dispute_date or recorded_date, rider_text=state.latest_message)
                fingerprint = (request.dispute_type, request.trip_id, request.dispute_date)
                if fingerprint in seen:
                    raise ValueError("Duplicate claims in one extraction are not allowed")
                seen.add(fingerprint)
                if existing is None:
                    existing = next((d for d in active.values() if (
                        d.dispute_type, d.trip_id, d.dispute_date) == fingerprint), None)
                if existing is not None:
                    if existing.dispute_id in revised:
                        raise ValueError("A dispute cannot be revised twice in one extraction")
                    revised.add(existing.dispute_id)
                    if session.scalar(select(PaymentAttempt.attempt_id).where(PaymentAttempt.dispute_id == existing.dispute_id)):
                        raise ValueError("Claims with a payment attempt require reconciliation, not revision")
                    stored = service.update_dispute(state.rider_id, conversation_id=state.conversation_id,
                        dispute_id=existing.dispute_id, claim=request, expected_revision=existing.revision)
                else:
                    stored = service.create_dispute(state.rider_id, conversation_id=state.conversation_id, claim=request)
                claims.append(DisputeRequest.model_validate(stored.claim))
            session.commit()
        return claims

    def _evaluate(self, state):
        results = []
        with self.context.session_factory() as session:
            for claim in state.claims:
                # Tool/model outputs are deliberately not accepted as financial evidence.
                result = DisputeService(session).evaluate(state.rider_id, claim, reference_date=self.context.now)
                stored = session.get(StoredDispute, claim.dispute_id)
                ConversationService(session).record_dispute_result(state.rider_id,
                    conversation_id=state.conversation_id, result=result, expected_revision=stored.revision)
                results.append(result)
            session.commit()
        return results

    def _policy(self, state, needs_information):
        if not state.validated_disputes:
            return [PolicyDecision(dispute_id=None,
                action=Action.ASK_FOR_INFO if needs_information else Action.NO_DISPUTE,
                reason="Provide a trip ID or the disputed IST date" if needs_information else "No payout dispute was identified")]
        payments, available = [], True
        if any(d.status == DisputeStatus.VALID for d in state.validated_disputes):
            def read_provider_ledger():
                with self.context.payswift_factory() as provider:
                    return traced_list_rider_payments(self.context.session_factory, provider, state.rider_id)
            try:
                payments = self._step(state, "policy_provider_ledger", {}, read_provider_ledger,
                    trace_output=lambda result: {"count": len(result),
                        "statuses": sorted({p.status for p in result}),
                        "total_amount": sum(p.amount for p in result)})
            except Exception:
                available = False
        with self.context.session_factory() as session:
            attempts = session.scalars(select(PaymentAttempt).where(PaymentAttempt.rider_id == state.rider_id)).all()
            return decide(state.validated_disputes, payments, attempts, self.context.now, ledger_available=available)

    def _execute(self, state, message_id):
        outcomes = []
        for index, decision in enumerate(state.policy_decision):
            output = {"dispute_id": decision.dispute_id, "action": decision.action.value}
            if decision.action == Action.AUTO_PAY:
                try:
                    receipt = self.payments.pay_dispute(state.rider_id, decision.dispute_id)
                    if receipt["status"] != "processed":
                        raise ValueError("Payment outcome requires reconciliation")
                    output["payment"] = receipt
                except Exception:
                    decision = PolicyDecision(dispute_id=decision.dispute_id, action=Action.ESCALATION,
                        amount=decision.amount, reason="Payment was not confirmed; ops must reconcile before further action")
                    state.policy_decision[index] = decision
                    output["action"] = decision.action.value
            if decision.action in {Action.OPS_APPROVAL, Action.ESCALATION}:
                with self.context.session_factory() as session:
                    ops_record = OpsService(session).enqueue(state.rider_id, state.conversation_id,
                        dispute_id=decision.dispute_id, message_id=message_id,
                        kind="approval" if decision.action == Action.OPS_APPROVAL else "escalation", reason=decision.reason)
                    session.commit()
                ops_name = "approval_creation" if decision.action == Action.OPS_APPROVAL else "escalation"
                ops_step = self.recorder.start(ops_name, {"dispute_id": decision.dispute_id}, kind="decision")
                self.recorder.finish(ops_step, output={"type": ops_record["type"], "amount": ops_record["amount"]})
                output.update(ops_record)
            elif decision.dispute_id and decision.action in {Action.AUTO_PAY, Action.NO_DISPUTE}:
                with self.context.session_factory() as session:
                    stored = session.get(StoredDispute, decision.dispute_id)
                    ConversationService(session).resolve_dispute(state.rider_id, conversation_id=state.conversation_id,
                        dispute_id=stored.dispute_id, expected_revision=stored.revision)
                    OpsService(session).mark_resolved(state.rider_id, stored.dispute_id)
                    session.commit()
            outcomes.append(output)
        return outcomes

    def _finish(self, state, message_id):
        disputes = {d.dispute_id: d for d in state.validated_disputes}
        replies = []
        for decision, outcome in zip(state.policy_decision, state.execution_results):
            dispute = disputes.get(decision.dispute_id)
            label = f"Trip {dispute.trip_id}: " if dispute and dispute.trip_id else (
                f"{dispute.dispute_type.value.replace('_', ' ')} on {dispute.dispute_date}: " if dispute else "")
            if decision.action == Action.ASK_FOR_INFO:
                replies.append(label + decision.reason + ".")
            elif decision.action == Action.AUTO_PAY:
                replies.append(label + dispute.explanation + f" Payment of ₹{outcome['payment']['amount']} was confirmed.")
            elif decision.action == Action.OPS_APPROVAL:
                replies.append(label + dispute.explanation + f" ₹{decision.amount} requires ops approval; it has been queued.")
            elif decision.action == Action.ESCALATION:
                replies.append(label + decision.reason + ". This has been queued for ops review.")
            else:
                replies.append(label + (dispute.explanation + " " if dispute else "") + decision.reason + ".")
        reply = "\n".join(replies)
        with self.context.session_factory() as session:
            service = ConversationService(session)
            service.append_message(state.rider_id, conversation_id=state.conversation_id, message_id=message_id,
                                   text=reply, role="assistant", received_at=self.context.now)
            conversation = service._conversation(state.rider_id, state.conversation_id)
            actions = {d.action for d in state.policy_decision}
            phase = "awaiting_review" if actions & {Action.OPS_APPROVAL, Action.ESCALATION} else (
                "awaiting_details" if Action.ASK_FOR_INFO in actions else "answered")
            service.update_conversation_state(state.rider_id, conversation_id=state.conversation_id,
                state=ConversationState(phase=phase, last_answer=reply,
                    focused_dispute_id=state.claims[0].dispute_id if state.claims else None),
                expected_version=conversation.version)
            session.commit()
        return reply
