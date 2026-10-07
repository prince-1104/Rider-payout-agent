from fastapi.testclient import TestClient


def test_health_returns_200_and_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "quickdrop-dispute-desk"}
