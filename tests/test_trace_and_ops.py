from fastapi.testclient import TestClient
from datetime import datetime, timezone

from app.db.models import Conversation, ConversationMessage, OpsQueueItem, StoredDispute
from app.api.ops import get_conversation_detail, list_conversations
from app.services.ops_service import OpsService

from app.services.trace import TraceRecorder, get_trace


def test_trace_is_empty_initially(client: TestClient) -> None:
    response = client.get("/trace/R003")

    assert response.status_code == 200
    assert response.json() == []


def test_ops_pending_is_empty_initially(client: TestClient) -> None:
    response = client.get("/ops/pending")

    assert response.status_code == 200
    assert response.json() == []


def test_trace_recorder_redacts_credentials(session_factory, session) -> None:
    recorder = TraceRecorder(session_factory, "R003")
    invocation = recorder.start("safe_audit", {"api_key": "sk-proj-1234567890", "message_id": "M1"},
                               kind="decision")
    recorder.finish(invocation, output={"authorization": "Bearer private-token", "result": "ok"})
    steps = get_trace(session, "R003")
    assert steps[0].input["api_key"] == "[REDACTED]"
    assert steps[0].output["authorization"] == "[REDACTED]"
    assert "private-token" not in str(steps)


def test_ops_conversation_detail_returns_ordered_messages_disputes_and_evidence(session):
    conversation = Conversation(conversation_id="conv-dashboard", rider_id="R003",
                                state={"phase": "awaiting_review"})
    session.add(conversation)
    session.flush()
    session.add_all([
        ConversationMessage(conversation_id=conversation.conversation_id, rider_id="R003",
                            message_id="msg-dashboard-1", role="rider", sequence=1,
                            text="My trip payout is short", received_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ConversationMessage(conversation_id=conversation.conversation_id, rider_id="R003",
                            message_id="msg-dashboard-2", role="assistant", sequence=2,
                            text="I am reviewing this", received_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)),
    ])
    session.add(StoredDispute(dispute_id="dispute-dashboard", conversation_id=conversation.conversation_id,
                              rider_id="R003", dispute_type="trip_payout", trip_id="T001", dispute_date=None,
                              claimed_amount=None, expected_amount=100, actual_amount=80, amount_owed=20,
                              status="valid", explanation="Verified shortfall", evidence={"trip_status": "completed"},
                              claim={"trip_id": "T001"}, revisions=[], revision=1, is_active=True))
    session.commit()

    body = get_conversation_detail("conv-dashboard", session)
    assert body["rider_id"] == "R003"
    assert [m["sequence"] for m in body["messages"]] == [1, 2]
    assert body["disputes"][0]["amount_owed"] == 20
    assert body["disputes"][0]["evidence"] == {"trip_status": "completed"}


def test_pending_items_include_dashboard_evidence_and_navigation_ids(session):
    conversation = Conversation(conversation_id="conv-queue", rider_id="R003", state={"phase": "awaiting_review"})
    session.add(conversation)
    session.flush()
    session.add(StoredDispute(dispute_id="dispute-queue", conversation_id="conv-queue", rider_id="R003",
                              dispute_type="trip_payout", trip_id="T001", dispute_date=None, claimed_amount=None,
                              expected_amount=100, actual_amount=80, amount_owed=20, status="valid",
                              explanation="Verified shortfall", evidence={"source": "dispatch"},
                              claim={"trip_id": "T001"}, revisions=[], revision=1, is_active=True))
    session.add(OpsQueueItem(id="queue-item", conversation_id="conv-queue", rider_id="R003",
                             dispute_id="dispute-queue", type="approval", amount=20, reason="Review shortfall"))
    session.commit()

    item = OpsService(session).list_pending()[0].model_dump(mode="json")
    assert item["dispute_type"] == "trip_payout"
    assert item["evidence"] == {"source": "dispatch"}
    assert item["dispute_id"] == "dispute-queue"
    assert item["conversation_id"] == "conv-queue"


def test_conversation_list_returns_only_operational_summary(session):
    conversation = Conversation(conversation_id="conv-summary", rider_id="R003",
                                state={"phase": "awaiting_review", "private_reasoning": "must not be returned"})
    session.add(conversation)
    session.flush()
    session.add(ConversationMessage(conversation_id="conv-summary", rider_id="R003",
        message_id="msg-summary", role="rider", sequence=1, text="Payout seems short",
        received_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    session.add(StoredDispute(dispute_id="dispute-summary", conversation_id="conv-summary", rider_id="R003",
        dispute_type="trip_payout", trip_id="T001", dispute_date=None, claimed_amount=None,
        expected_amount=100, actual_amount=80, amount_owed=20, status="valid", explanation="Shortfall",
        evidence={}, claim={"trip_id": "T001"}, revisions=[], revision=1, is_active=True))
    session.commit()

    result = list_conversations(session, rider_id="r003", status="awaiting_review")

    assert len(result) == 1
    summary = result[0].model_dump()
    assert summary["rider_id"] == "R003"
    assert summary["active_dispute_count"] == 1
    assert summary["latest_message_preview"] == "Payout seems short"
    assert summary["state"] == {"phase": "awaiting_review"}
    assert "private_reasoning" not in str(summary)
