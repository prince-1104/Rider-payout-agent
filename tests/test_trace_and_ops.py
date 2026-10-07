from fastapi.testclient import TestClient


def test_trace_is_empty_initially(client: TestClient) -> None:
    response = client.get("/trace/R003")

    assert response.status_code == 200
    assert response.json() == []


def test_ops_pending_is_empty_initially(client: TestClient) -> None:
    response = client.get("/ops/pending")

    assert response.status_code == 200
    assert response.json() == []
