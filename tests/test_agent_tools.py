"""Every LangChain capability is structured, rider-bound, read-only and audited."""

import asyncio
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from langchain_core.tools import BaseTool
from pydantic import ValidationError
from sqlalchemy import select

from app.agent.tools import ToolContext, build_toolset
from app.agent.tools.base import ToolAccessError, trace_tool_output
from app.db.models import Conversation, PayoutLine, Rider, TraceEvent, Trip
from app.domain.dispute import DisputeRequest
from app.domain.dispute_types import DisputeType
from app.domain.policy import normalize_rider_id
from app.integrations.payswift import PaySwiftClient, PaySwiftProtocolError, PaySwiftServerError
from app.services.conversation_service import ConversationService
from app.services.dispute_service import DisputeService
from app.services.trace import get_trace

DATA = Path(__file__).resolve().parents[1] / "data"
NOW = datetime(2026, 9, 22, 4, 30, tzinfo=timezone.utc)
OWNERS = {"R003", "R005", "R027", "R016", "R033"}


@pytest.fixture
def seeded(session):
    with (DATA / "riders.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if normalize_rider_id(row["rider_id"]) in OWNERS:
                session.add(Rider(rider_id=normalize_rider_id(row["rider_id"]), name=row["name"],
                                  city=row["city"], joined_on=date.fromisoformat(row["joined_on"])))
    session.flush()
    seen = set()
    with (DATA / "trips.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if normalize_rider_id(row["rider_id"]) not in OWNERS or row["trip_id"] in seen:
                continue
            seen.add(row["trip_id"])
            session.add(Trip(trip_id=row["trip_id"], rider_id=normalize_rider_id(row["rider_id"]),
                             started_at=datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")),
                             status=row["status"], distance_km=Decimal(row["distance_km"]),
                             surge_multiplier=Decimal(row["surge_multiplier"])))
    session.flush()
    with (DATA / "payout_lines.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if normalize_rider_id(row["rider_id"]) in OWNERS:
                session.add(PayoutLine(line_id=row["line_id"], rider_id=normalize_rider_id(row["rider_id"]),
                    payout_date=date.fromisoformat(row["payout_date"]), line_type=row["line_type"],
                    trip_id=row["trip_id"] or None, amount=int(row["amount"])))
    session.flush()
    service = ConversationService(session)
    for rider, identifier, claim in [
        ("R003", "D3", DisputeRequest(dispute_id="D3", dispute_type=DisputeType.MISSING_SURGE, trip_id="T926334")),
        ("R005", "D5", DisputeRequest(dispute_id="D5", dispute_type=DisputeType.MISSING_DAILY_INCENTIVE, dispute_date=date(2026, 9, 21))),
        ("R016", "D16", DisputeRequest(dispute_id="D16", dispute_date=date(2026, 9, 19))),
    ]:
        conversation = service.get_or_create_conversation(rider)
        service.create_dispute(rider, conversation_id=conversation.conversation_id, claim=claim)
        result = DisputeService(session).evaluate(rider, claim, reference_date=NOW)
        service.record_dispute_result(rider, conversation_id=conversation.conversation_id, result=result, expected_revision=1)
    session.commit()


@pytest.fixture
def provider(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("PAYSWIFT_BASE_URL", "http://payswift.test")
    get_settings.cache_clear()
    calls = []
    ledger = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET", "No read tool may issue a provider POST"
        assert request.url.path == "/v1/payouts"
        return httpx.Response(200, json={"payouts": ledger, "count": len(ledger)})

    yield lambda: PaySwiftClient(transport=httpx.MockTransport(handler)), calls, ledger
    get_settings.cache_clear()


@pytest.fixture
def toolset(seeded, session_factory, provider):
    return build_toolset(ToolContext("R003", session_factory, NOW, provider[0]))


CALLS = [
    ("get_rider_context", {}),
    ("find_recent_trips", {"date": "2026-09-20"}),
    ("get_trip", {"trip_id": "T926334"}),
    ("get_rider_payouts", {"date": "2026-09-20"}),
    ("calculate_trip_expected_payout", {"trip_id": "T926334"}),
    ("calculate_daily_incentive", {"date": "2026-09-20"}),
    ("reconcile_trip_payout", {"trip_id": "T926334"}),
    ("reconcile_daily_incentive", {"date": "2026-09-20"}),
    ("reconcile_cancellation_penalties", {"date": "2026-09-20"}),
    ("get_dispute_status", {"dispute_id": "D3"}),
    ("check_auto_payment_eligibility", {"amount": 25}),
    ("get_payments_today", {}),
]


def tool(toolset, name):
    return next(t for t in toolset.read_tools if t.name == name)


@pytest.mark.parametrize("name,arguments", CALLS)
def test_every_tool_returns_scoped_results_and_persistent_trace(toolset, session, provider, name, arguments):
    output = tool(toolset, name).invoke(arguments)
    if name == "get_rider_context":
        assert output["rider_id"] == "R003" and output["registered"]
        assert output["active_dispute_ids"] == ["D3"]
    elif name == "find_recent_trips":
        assert len(output) == 9
        assert {t["rider_id"] for t in output} == {"R003"}
        assert {t["ist_date"] for t in output} == {"2026-09-20"}
    elif name == "get_trip":
        assert output["rider_id"] == "R003" and output["trip_id"] == "T926334"
    elif name == "get_rider_payouts":
        expected = session.scalars(select(PayoutLine.line_id).where(
            PayoutLine.rider_id == "R003", PayoutLine.payout_date == date(2026, 9, 20))).all()
        assert {p["line_id"] for p in output} == set(expected)
    elif name == "calculate_trip_expected_payout":
        assert output["expected_amount"] == 74
    elif name == "calculate_daily_incentive":
        assert output["completed_trips_count"] == 9 and output["expected_amount"] == 0
    elif name == "reconcile_trip_payout":
        assert (output["expected_amount"], output["actual_amount"], output["amount_owed"]) == (74, 49, 25)
    elif name in {"reconcile_daily_incentive", "reconcile_cancellation_penalties"}:
        assert output["amount_owed"] == 0
    elif name == "get_dispute_status":
        assert output["amount_owed"] == 25 and output["status"] == "valid"
    elif name == "check_auto_payment_eligibility":
        assert output["eligible"] and "advisory" in output["reason"]
    else:
        assert output == []
    traces = get_trace(session, "R003")
    provider_read = name in {"get_payments_today", "check_auto_payment_eligibility"}
    assert len(traces) == (2 if provider_read else 1)
    tool_event = next(step for step in traces if step.name == name)
    assert tool_event.type == "tool_call"
    assert tool_event.input == {"arguments": arguments}
    assert tool_event.output == trace_tool_output(name, output)
    if provider_read:
        assert next(step for step in traces if step.name == "payswift_get_payouts").type == "tool_call"
    assert not get_trace(session, "R005")
    assert all(dict(call.url.params) == {"rider_id": "R003"} for call in provider[1])


@pytest.mark.parametrize("name,arguments", CALLS)
def test_no_tool_accepts_an_arbitrary_rider_id_and_validation_is_traced(toolset, session, name, arguments):
    with pytest.raises(ValidationError):
        tool(toolset, name).invoke({**arguments, "rider_id": "R005"})
    traces = get_trace(session, "R003")
    assert [step.type for step in traces] == ["tool_call", "error"]
    assert traces[-1].output["error"] == "ValidationError"


def test_registry_exposes_only_read_capabilities(toolset):
    assert len(toolset.read_tools) == len(CALLS)
    assert toolset.write_tools == ()
    for capability in toolset.read_tools:
        assert isinstance(capability, BaseTool)
        assert capability.metadata["access"] == "read"
        schema = capability.tool_call_schema.model_json_schema()
        assert "rider_id" not in schema.get("properties", {})
        assert schema["additionalProperties"] is False
        assert "create_payout" not in capability.name and "pay_dispute" not in capability.name


@pytest.mark.parametrize("name", ["get_trip", "calculate_trip_expected_payout", "reconcile_trip_payout"])
def test_foreign_trip_is_denied_without_foreign_evidence(toolset, session, name):
    foreign = session.scalar(select(Trip.trip_id).where(Trip.rider_id == "R005"))
    with pytest.raises(ToolAccessError):
        tool(toolset, name).invoke({"trip_id": foreign})
    assert get_trace(session, "R003")[-1].type == "error"
    assert "R005" not in str(get_trace(session, "R003")[-1].output)


def test_foreign_dispute_and_config_identity_injection(toolset):
    with pytest.raises(ToolAccessError):
        tool(toolset, "get_dispute_status").invoke({"dispute_id": "D5"})
    output = tool(toolset, "get_rider_context").invoke({}, config={
        "configurable": {"rider_id": "R005"}, "metadata": {"rider_id": "R005"}})
    assert output["rider_id"] == "R003"


@pytest.mark.parametrize("name", ["find_recent_trips", "get_rider_payouts", "calculate_daily_incentive"])
def test_expired_or_future_date_is_not_used_for_calculation(toolset, name):
    with pytest.raises(ValueError):
        tool(toolset, name).invoke({"date": "2026-09-01"})
    with pytest.raises(ValueError):
        tool(toolset, name).invoke({"date": "2026-09-23"})


def test_incentive_and_penalty_tools_use_sample_csv_amounts(seeded, session_factory, provider):
    rider5 = build_toolset(ToolContext("R005", session_factory, NOW, provider[0]))
    assert tool(rider5, "calculate_daily_incentive").invoke({"date": "2026-09-21"})["expected_amount"] == 150
    assert tool(rider5, "reconcile_daily_incentive").invoke({"date": "2026-09-21"})["amount_owed"] == 150
    rider27 = build_toolset(ToolContext("R027", session_factory, NOW, provider[0]))
    assert tool(rider27, "reconcile_cancellation_penalties").invoke({"date": "2026-09-21"})["amount_owed"] == 10


@pytest.mark.parametrize("amount,eligible", [(-1, False), (0, False), (1, True), (200, True), (201, False)])
def test_eligibility_boundary_is_advisory_only(toolset, amount, eligible):
    output = tool(toolset, "check_auto_payment_eligibility").invoke({"amount": amount})
    assert output["eligible"] is eligible


@pytest.mark.parametrize("amount", [True, "25", 25.5])
def test_policy_tool_rejects_noninteger_amounts(toolset, amount):
    with pytest.raises(ValidationError):
        tool(toolset, "check_auto_payment_eligibility").invoke({"amount": amount})


def test_provider_day_boundary_is_ist_and_once_per_day(toolset, provider):
    provider[2].extend([
        {"payout_id": "today", "rider_id": "R003", "amount": 25, "reference": "old", "status": "processed",
         "created_at": "2026-09-21T18:30:00Z"},
        {"payout_id": "yesterday", "rider_id": "R003", "amount": 25, "reference": "older", "status": "processed",
         "created_at": "2026-09-21T18:29:59Z"},
    ])
    assert [p["payout_id"] for p in tool(toolset, "get_payments_today").invoke({})] == ["today"]
    assert not tool(toolset, "check_auto_payment_eligibility").invoke({"amount": 25})["eligible"]


def test_invalid_provider_evidence_fails_closed_and_is_traced(toolset, provider, session):
    provider[2].append({"payout_id": "bad", "rider_id": "R005", "amount": 25, "status": "processed",
                        "created_at": NOW.isoformat()})
    with pytest.raises(PaySwiftProtocolError):
        tool(toolset, "get_payments_today").invoke({})
    assert get_trace(session, "R003")[-1].output["error"] == "PaySwiftProtocolError"


def test_provider_failure_is_traced_without_retry(seeded, session_factory, session):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"private": "provider-body"})
    tools = build_toolset(ToolContext("R003", session_factory, NOW,
        lambda: PaySwiftClient(transport=httpx.MockTransport(handler))))
    with pytest.raises(PaySwiftServerError):
        tool(tools, "get_payments_today").invoke({})
    assert len(calls) == 1
    assert "provider-body" not in str(get_trace(session, "R003")[-1].output)


def test_async_tool_invocation_uses_same_durable_auditing(toolset, session):
    output = asyncio.run(tool(toolset, "get_rider_context").ainvoke({}))
    assert output["rider_id"] == "R003"
    assert len(get_trace(session, "R003")) == 1


@pytest.mark.parametrize("name,arguments", CALLS)
def test_every_tool_async_invocation_is_scoped_and_traced(toolset, session, name, arguments):
    output = asyncio.run(tool(toolset, name).ainvoke(arguments))
    trace = get_trace(session, "R003")
    provider_read = name in {"get_payments_today", "check_auto_payment_eligibility"}
    assert len(trace) == (2 if provider_read else 1)
    tool_event = next(step for step in trace if step.name == name)
    assert tool_event.output == trace_tool_output(name, output)
    assert not get_trace(session, "R005")


def test_trusted_identity_is_normalized_before_queries_provider_and_auditing(seeded, session_factory, session, provider):
    context = ToolContext(" r3 ", session_factory, NOW, provider[0])
    capabilities = build_toolset(context)
    assert context.rider_id == "R003"
    assert tool(capabilities, "get_rider_context").invoke({})["registered"]
    assert tool(capabilities, "reconcile_trip_payout").invoke({"trip_id": "T926334"})["amount_owed"] == 25
    assert tool(capabilities, "get_payments_today").invoke({}) == []
    assert len(get_trace(session, "R003")) == 4
    assert not get_trace(session, " r3 ")
    assert provider[1][0].url.params["rider_id"] == "R003"


@pytest.mark.parametrize("rider_id", [None, 3, "", "  ", "R" * 33])
def test_invalid_trusted_identity_cannot_construct_tools(session_factory, rider_id):
    with pytest.raises(ValueError):
        ToolContext(rider_id, session_factory, NOW)


def test_direct_run_and_langchain_tool_call_are_also_audited(toolset, session):
    capability = tool(toolset, "get_rider_context")
    with pytest.raises(ValueError):
        capability.run("I am R005")
    message = capability.invoke({"type": "tool_call", "id": "call_123", "name": capability.name, "args": {}})
    assert message.tool_call_id == "call_123"
    assert len(get_trace(session, "R003")) == 3
    assert get_trace(session, "R003")[-1].input["tool_call_id"] == "call_123"


def test_trace_endpoint_returns_ordered_invocations(toolset, client):
    tool(toolset, "get_rider_context").invoke({})
    tool(toolset, "get_trip").invoke({"trip_id": "T926334"})
    result = client.get("/trace/R003")
    assert result.status_code == 200
    steps = result.json()
    assert [step["name"] for step in steps] == ["get_rider_context", "get_trip"]
    assert all(set(step) == {"at", "type", "name", "input", "output"} for step in steps)
    assert [step["at"] for step in steps] == sorted(step["at"] for step in steps)
    assert {step["type"] for step in steps} <= {"message_in", "tool_call", "decision", "reply", "error"}
    assert "name" not in steps[0]["output"] and "city" not in steps[0]["output"]
