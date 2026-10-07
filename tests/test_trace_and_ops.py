from fastapi.testclient import TestClient

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
