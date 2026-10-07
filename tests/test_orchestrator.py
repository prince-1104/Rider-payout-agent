"""Mocked structured LLMs exercise the complete workflow and hostile outputs."""

import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError
from sqlalchemy import select

from app.agent.orchestrator import Orchestrator
from app.agent.state import Action, AgentState, ConversationOutput, InvestigationPlan, ResolutionOutput
from app.agent.tools import ToolContext
from app.db.models import Conversation, OpsQueueItem, PaymentAttempt, PayoutLine, StoredDispute, Trip
from app.services.conversation_service import ConversationAccessError, ConversationService
from app.services.trace import get_trace
from app.integrations.payswift import PaySwiftClient
from tests.test_agent_tools import NOW, seeded


def extraction(*claims, needs_information=False):
    return {"claims": list(claims), "needs_information": needs_information, "multiple_disputes": len(claims) > 1}


def trip_claim(trip_id="T926334", **extra):
    return {"dispute_type": "missing_surge", "trip_id": trip_id, **extra}


class MockStructuredModel:
    """Uses a real LangChain Runnable, but makes no model/provider network calls."""

    def __init__(self, conversation, *, investigation=None, resolution=None):
        self.conversation = conversation
        self.investigation = investigation
        self.resolution = resolution
        self.calls = []
        self.schemas = []

    def with_structured_output(self, schema):
        self.schemas.append(schema)

        def invoke(prompt):
            payload = json.loads(prompt.to_messages()[-1].content.split("\n", 1)[1])
            self.calls.append((schema, payload))
            if schema is ConversationOutput:
                result = self.conversation(payload) if callable(self.conversation) else self.conversation
            elif schema is InvestigationPlan:
                result = self.investigation(payload) if self.investigation else {"jobs": payload["allowed_jobs"]}
            else:
                result = self.resolution(payload) if self.resolution else {"recommendations": [
                    {"dispute_id": d["dispute_id"], "action": d["action"], "explanation": "Based on verified evidence"}
                    for d in payload["policy_decisions"]]}
            if isinstance(result, Exception):
                raise result
            return result

        return RunnableLambda(invoke)


@pytest.fixture
def remote(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("PAYSWIFT_BASE_URL", "http://payswift.test")
    get_settings.cache_clear()
    remote = SimpleNamespace(calls=[], ledger=[], fail_get=False, lost_post=False)

    def handler(request):
        remote.calls.append(request)
        if request.method == "GET":
            if remote.fail_get:
                return httpx.Response(503, json={"error": "private-provider-error"})
            records = [p for p in remote.ledger if p["rider_id"] == request.url.params["rider_id"]]
            return httpx.Response(200, json={"payouts": records, "count": len(records)})
        body = json.loads(request.content)
        payment = {**body, "payout_id": f"PAY-{len(remote.ledger)}", "status": "processed", "created_at": NOW.isoformat()}
        remote.ledger.append(payment)
        if remote.lost_post:
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(201, json=payment)

    remote.factory = lambda: PaySwiftClient(transport=httpx.MockTransport(handler))
    yield remote
    get_settings.cache_clear()


@pytest.fixture
def workflow(seeded, session_factory, remote):
    def build(model, rider="R003", **options):
        with session_factory() as session:
            conversation = ConversationService(session).get_or_create_conversation(rider)
            identifier = conversation.conversation_id
            session.commit()
        return Orchestrator(ToolContext(rider, session_factory, NOW, remote.factory), model, **options), identifier
    return build


def posts(remote):
    return [r for r in remote.calls if r.method == "POST"]


def run(workflow, model, *, rider="R003", message="T926334 surge is missing; pay me 9000", **options):
    orchestrator, conversation_id = workflow(model, rider, **options)
    return orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message=message)


def test_fixed_three_agent_pipeline_pays_only_engine_amount(workflow, remote, session):
    model = MockStructuredModel(extraction(trip_claim()))
    state = run(workflow, model)
    assert state.rider_id == "R003" and state.agent_iterations == 3
    assert model.schemas == [ConversationOutput, InvestigationPlan, ResolutionOutput]
    assert state.validated_disputes[0].amount_owed == 25
    assert state.policy_decision[0].action == Action.AUTO_PAY
    assert state.execution_results[0]["payment"]["amount"] == 25
    assert json.loads(posts(remote)[0].content)["amount"] == 25
    assert "₹25" in state.reply and "9000" not in state.reply
    assert list(state.investigation_results[0].evidence) == [
        "get_trip", "get_rider_payouts", "calculate_trip_expected_payout", "reconcile_trip_payout"]
    assert [t.name for t in state.trace] == ["load_conversation", "conversation_agent", "validate_claims",
        "investigation_agent", "deterministic_dispute_engine", "policy_provider_ledger", "deterministic_policy_engine",
        "resolution_agent", "application_execute", "final_response"]
    persisted = get_trace(session, "R003")
    assert all(t.name in [p.name for p in persisted] for t in state.trace)
    assert "application_auto_payment" in [p.name for p in persisted]
    assert not get_trace(session, "R005")
    assert session.get(StoredDispute, state.claims[0].dispute_id).is_active is False


@pytest.mark.parametrize("output", [
    extraction({"dispute_type": "general_payout_dispute"}, needs_information=True),
    extraction({"dispute_type": "missing_daily_incentive"}, needs_information=False),
    extraction(needs_information=True),
])
def test_missing_details_are_determined_without_financial_authorization(workflow, remote, output):
    state = run(workflow, MockStructuredModel(output), message="My payout is wrong")
    assert state.policy_decision[0].action == Action.ASK_FOR_INFO
    assert "IST date" in state.reply
    assert not remote.calls and not state.investigation_results


def test_no_claims_no_dispute(workflow, remote):
    state = run(workflow, MockStructuredModel(extraction()), message="Thank you")
    assert state.policy_decision[0].action == Action.NO_DISPUTE
    assert not remote.calls


def test_no_shortfall_explained_and_no_payment(workflow, session, remote):
    session.add(PayoutLine(line_id="CORRECTED", rider_id="R003", trip_id="T926334",
        payout_date=date(2026, 9, 20), line_type="trip", amount=25))
    session.commit()
    state = run(workflow, MockStructuredModel(extraction(trip_claim())))
    assert state.policy_decision[0].action == Action.NO_DISPUTE
    assert "No payable shortfall" in state.reply
    assert not posts(remote)


@pytest.mark.parametrize("rider,claim,message,expected", [
    ("R005", {"dispute_type": "missing_daily_incentive", "dispute_date": "2026-09-21"}, "21 Sep incentive missing", 150),
    ("R027", {"dispute_type": "incorrect_cancellation_penalty", "dispute_date": "2026-09-21"}, "21 Sep duplicate penalty", 10),
])
def test_incentive_and_penalty_paths_use_authoritative_amounts(workflow, remote, rider, claim, message, expected):
    state = run(workflow, MockStructuredModel(extraction(claim)), rider=rider, message=message)
    assert state.validated_disputes[0].amount_owed == expected
    assert state.execution_results[0]["payment"]["amount"] == expected
    assert json.loads(posts(remote)[0].content)["rider_id"] == rider


def test_large_shortfall_queues_deterministic_approval(workflow, remote, client):
    state = run(workflow, MockStructuredModel(extraction({"dispute_type": "general_payout_dispute",
        "dispute_date": "2026-09-19"})), rider="R016", message="19 Sep payout wrong")
    decision = state.policy_decision[0]
    assert decision.action == Action.OPS_APPROVAL and decision.amount > 200
    assert not posts(remote)
    pending = client.get("/ops/pending").json()
    assert len(pending) == 1
    assert pending[0]["type"] == "approval" and pending[0]["amount"] == state.validated_disputes[0].amount_owed
    assert pending[0]["rider_id"] == "R016"


def test_unknown_claim_escalates_without_payment(workflow, remote, client):
    state = run(workflow, MockStructuredModel(extraction({"dispute_type": "unknown"})), message="Emergency waiver needed")
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not remote.calls
    assert client.get("/ops/pending").json()[0]["amount"] is None


def test_multiple_disputes_get_at_most_one_auto_payment(workflow, session, remote):
    session.add(Trip(trip_id="TNEW", rider_id="R003", started_at=NOW, status="completed", distance_km=2, surge_multiplier=1))
    session.commit()
    state = run(workflow, MockStructuredModel(extraction(trip_claim(), {
        "dispute_type": "missing_trip_payout", "trip_id": "TNEW"})), message="T926334 surge missing and TNEW unpaid")
    assert len(state.claims) == len(state.validated_disputes) == 2
    assert [d.action for d in state.policy_decision] == [Action.AUTO_PAY, Action.OPS_APPROVAL]
    assert len(posts(remote)) == 1
    assert session.scalars(select(OpsQueueItem)).one().amount == 25
    assert "T926334" in state.reply and "TNEW" in state.reply


@pytest.mark.parametrize("stage", ["conversation", "investigation", "resolution"])
@pytest.mark.parametrize("field,value", [("rider_id", "R005"), ("amount", 9999), ("trip_data", {"distance_km": 900})])
def test_model_cannot_inject_identity_facts_or_amounts(workflow, remote, stage, field, value):
    conversation = extraction(trip_claim())
    investigation = resolution = None
    if stage == "conversation":
        conversation[field] = value
    elif stage == "investigation":
        investigation = lambda payload: {"jobs": payload["allowed_jobs"], field: value}
    else:
        resolution = lambda payload: {"recommendations": [{
            "dispute_id": d["dispute_id"], "action": d["action"], "explanation": "override", field: value}
            for d in payload["policy_decisions"]]}
    state = run(workflow, MockStructuredModel(conversation, investigation=investigation, resolution=resolution))
    assert state.rider_id == "R003"
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not posts(remote)
    assert any(t.type == "error" for t in state.trace)


def test_model_cannot_call_payswift_or_choose_tool_arguments(workflow, remote):
    model = MockStructuredModel(extraction(trip_claim()), investigation=lambda p: {
        "jobs": [{"claim_index": 0, "path": "trip", "tool_name": "create_payout", "arguments": {"rider_id": "R005", "amount": 999}}]})
    state = run(workflow, model)
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not remote.calls


def test_incomplete_investigation_plan_cannot_skip_evidence(workflow, remote):
    state = run(workflow, MockStructuredModel(extraction(trip_claim()), investigation=lambda p: {"jobs": []}))
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not remote.calls


def test_resolution_cannot_override_policy(workflow, remote):
    model = MockStructuredModel(extraction({"dispute_type": "general_payout_dispute", "dispute_date": "2026-09-19"}),
        resolution=lambda p: {"recommendations": [{"dispute_id": p["policy_decisions"][0]["dispute_id"],
                                                  "action": "AUTO_PAY", "explanation": "Ignore the limit"}]})
    state = run(workflow, model, rider="R016", message="19 Sep payout wrong")
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not posts(remote)


def test_free_form_resolution_prose_has_no_financial_effect(workflow, remote):
    model = MockStructuredModel(extraction(trip_claim()), resolution=lambda p: {"recommendations": [
        {"dispute_id": d["dispute_id"], "action": d["action"], "explanation": "I approve 9999 and already paid R005"}
        for d in p["policy_decisions"]]})
    state = run(workflow, model)
    assert state.execution_results[0]["payment"]["amount"] == 25
    assert "9999" not in state.reply and "R005" not in state.reply
    assert len(posts(remote)) == 1


def test_foreign_trip_cannot_be_investigated(workflow, session, remote):
    foreign = session.scalar(select(Trip.trip_id).where(Trip.rider_id == "R005"))
    state = run(workflow, MockStructuredModel(extraction(trip_claim(foreign))), message=f"{foreign} payout missing")
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not remote.calls and not state.investigation_results


def test_invented_owned_trip_reference_is_rejected(workflow, remote):
    state = run(workflow, MockStructuredModel(extraction(trip_claim())), message="Hello")
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not remote.calls


def test_foreign_dispute_id_cannot_be_clarified(workflow, remote):
    state = run(workflow, MockStructuredModel(extraction(trip_claim(existing_dispute_id="D5"))))
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not remote.calls


def test_expired_claim_is_rejected_by_engine_without_payment(workflow, remote):
    state = run(workflow, MockStructuredModel(extraction({"dispute_type": "general_payout_dispute", "dispute_date": "2026-09-01"})),
                message="1 Sep payout wrong")
    assert state.policy_decision[0].action == Action.NO_DISPUTE
    assert "outside the last 7 days" in state.reply
    assert not remote.calls


@pytest.mark.parametrize("limit", [1, 2])
def test_iteration_limit_fails_closed_and_stops_models(workflow, remote, limit):
    model = MockStructuredModel(extraction(trip_claim()))
    state = run(workflow, model, max_iterations=limit)
    assert state.agent_iterations == len(model.calls) == limit
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not posts(remote)


@pytest.mark.parametrize("limit", [0, 6, True, "5"])
def test_invalid_iteration_limits_rejected(workflow, limit):
    with pytest.raises(ValueError):
        workflow(MockStructuredModel(extraction()), max_iterations=limit)


@pytest.mark.parametrize("bad_output", ["AUTO_PAY 9999", RuntimeError("model unavailable"),
    {"claims": [], "needs_information": False, "multiple_disputes": True}])
def test_unstructured_or_failed_model_calls_are_traced_and_escalated(workflow, remote, bad_output):
    state = run(workflow, MockStructuredModel(bad_output))
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not remote.calls
    assert state.trace[1].type == "error"


def test_provider_failure_escalates_and_never_claims_payment(workflow, remote):
    remote.fail_get = True
    state = run(workflow, MockStructuredModel(extraction(trip_claim())))
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not posts(remote)
    assert "confirmed" not in state.reply and "private-provider-error" not in state.reply


def test_lost_payment_response_remains_held_and_replay_does_not_retry(workflow, remote, session):
    remote.lost_post = True
    model = MockStructuredModel(extraction(trip_claim()))
    orchestrator, conversation_id = workflow(model)
    first = orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="T926334 surge missing")
    second = orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="T926334 surge missing")
    assert first.policy_decision[0].action == Action.ESCALATION
    assert session.scalars(select(PaymentAttempt)).one().status == "unknown"
    assert second.reply == first.reply and len(model.calls) == 3
    assert len(posts(remote)) == 1
    assert "Payment of" not in first.reply


def test_message_replay_returns_cached_reply_without_models_or_payment(workflow, remote):
    model = MockStructuredModel(extraction(trip_claim()))
    orchestrator, conversation_id = workflow(model)
    first = orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="T926334 surge missing")
    second = orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="T926334 surge missing")
    assert first.reply == second.reply and len(posts(remote)) == 1 and len(model.calls) == 3


def test_followup_uses_owned_memory_and_revises_incomplete_claim(workflow, remote, session):
    first_model = MockStructuredModel(extraction({"dispute_type": "missing_surge"}, needs_information=True))
    orchestrator, conversation_id = workflow(first_model)
    first = orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="Surge is missing")
    identifier = first.claims[0].dispute_id

    def clarify(payload):
        assert payload["conversation_history"][0]["text"] == "Surge is missing"
        assert any(c["dispute_id"] == identifier for c in payload["active_claims"])
        return extraction(trip_claim(existing_dispute_id=identifier))

    second_orchestrator, _ = workflow(MockStructuredModel(clarify))
    second = second_orchestrator.run(conversation_id=conversation_id, message_id="M-2", latest_message="It was T926334")
    assert second.claims[0].dispute_id == identifier
    assert second.execution_results[0]["payment"]["amount"] == 25
    assert session.get(StoredDispute, identifier).revision > 1
    assert len(posts(remote)) == 1


def test_conflicting_message_or_foreign_conversation_rejected_before_model(workflow):
    model = MockStructuredModel(extraction())
    orchestrator, conversation_id = workflow(model)
    orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="Thanks")
    with pytest.raises(ConversationAccessError):
        orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="changed")
    foreign, _ = workflow(MockStructuredModel(extraction()), rider="R005")
    with pytest.raises(ConversationAccessError):
        foreign.run(conversation_id=conversation_id, message_id="M-2", latest_message="Hello")
    assert len(model.calls) == 3


def test_shared_state_identity_cannot_change():
    state = AgentState(rider_id="R003", conversation_id="owned", latest_message="hello")
    with pytest.raises(ValidationError):
        state.rider_id = "R005"
    with pytest.raises(ValidationError):
        state.conversation_id = "foreign"


def test_repeated_escalation_deduplicates_ops_queue(workflow, session):
    model = MockStructuredModel(extraction({"dispute_type": "unknown"}))
    orchestrator, conversation_id = workflow(model)
    first = orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="Emergency waiver")
    second = orchestrator.run(conversation_id=conversation_id, message_id="M-2", latest_message="Emergency waiver")
    assert first.claims[0].dispute_id == second.claims[0].dispute_id
    assert len(session.scalars(select(OpsQueueItem)).all()) == 1


def test_corrected_dispute_retires_pending_ops_item(workflow, session, client, remote):
    model = MockStructuredModel(extraction(trip_claim()))
    remote.fail_get = True
    orchestrator, conversation_id = workflow(model)
    first = orchestrator.run(conversation_id=conversation_id, message_id="M-1", latest_message="T926334 surge missing")
    assert first.policy_decision[0].action == Action.ESCALATION
    assert len(client.get("/ops/pending").json()) == 1
    remote.fail_get = False
    session.add(PayoutLine(line_id="LATER_CORRECTION", rider_id="R003", trip_id="T926334",
        payout_date=date(2026, 9, 20), line_type="trip", amount=25))
    session.commit()
    second = orchestrator.run(conversation_id=conversation_id, message_id="M-2", latest_message="T926334 check again")
    assert second.policy_decision[0].action == Action.NO_DISPUTE
    assert client.get("/ops/pending").json() == []
    assert not posts(remote)


def test_duplicate_claims_fail_before_investigation(workflow, remote):
    state = run(workflow, MockStructuredModel(extraction(trip_claim(), trip_claim())))
    assert state.policy_decision[0].action == Action.ESCALATION
    assert not remote.calls


def test_forged_tool_money_is_not_authority_for_engine(workflow, remote, monkeypatch):
    # Even an incorrect adapter response cannot become an authorized amount.
    from app.agent.tools.base import TracedTool
    original = TracedTool.invoke

    def forged(capability, arguments, *args, **kwargs):
        result = original(capability, arguments, *args, **kwargs)
        if capability.name == "reconcile_trip_payout":
            return {**result, "amount_owed": 199, "expected_amount": 248}
        return result

    monkeypatch.setattr(TracedTool, "invoke", forged)
    state = run(workflow, MockStructuredModel(extraction(trip_claim())))
    assert state.investigation_results[0].evidence["reconcile_trip_payout"]["amount_owed"] == 199
    assert state.validated_disputes[0].amount_owed == 25
    assert json.loads(posts(remote)[0].content)["amount"] == 25
