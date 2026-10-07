from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import InboundMessage


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message_id": "wamid.KAZ5TCF7RARUVH",
        "rider_id": "R003",
        "text": "Bhai order T926334 ka surge nahi mila, 20 tarikh wala",
        "received_at": "2026-09-22T09:05:00+05:30",
    }
    body.update(overrides)
    return body


def test_valid_message_without_model_returns_durable_escalation(
    client: TestClient, session: Session
) -> None:
    response = client.post("/messages", json=_payload())

    assert response.status_code == 200
    assert set(response.json()) == {"reply"}
    assert "queued for ops review" in response.json()["reply"]

    stored = session.scalars(select(InboundMessage)).one()
    assert stored.message_id == "wamid.KAZ5TCF7RARUVH"
    assert stored.rider_id == "R003"
    assert stored.reply == response.json()["reply"]
    assert stored.workflow_state["policy_decision"][0]["action"] == "ESCALATION"


@pytest.mark.parametrize("missing", ["message_id", "rider_id", "text", "received_at"])
def test_missing_required_field_is_rejected(client: TestClient, missing: str) -> None:
    body = _payload()
    del body[missing]

    response = client.post("/messages", json=body)

    assert response.status_code == 422
    assert any(err["loc"][-1] == missing for err in response.json()["detail"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message_id", ""),
        ("message_id", "   "),
        ("rider_id", ""),
        ("message_id", "w" * 129),
        ("text", "x" * 4097),
        ("received_at", "not-a-date"),
        ("received_at", "2026-09-22T09:05:00"),  # naive timestamp: timezone is required
        ("text", 123),
        ("rider_id", None),
    ],
)
def test_invalid_field_values_are_rejected(client: TestClient, field: str, value: Any) -> None:
    response = client.post("/messages", json=_payload(**{field: value}))

    assert response.status_code == 422


def test_non_json_body_is_rejected(client: TestClient) -> None:
    response = client.post("/messages", content=b"not json", headers={"Content-Type": "application/json"})

    assert response.status_code == 422


def test_unknown_fields_are_ignored(client: TestClient) -> None:
    response = client.post("/messages", json=_payload(vendor_extra="ignored"))

    assert response.status_code == 200


def test_duplicate_delivery_replays_reply_without_new_row(client: TestClient, session: Session) -> None:
    first = client.post("/messages", json=_payload())
    second = client.post("/messages", json=_payload())

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    count = session.scalar(select(func.count()).select_from(InboundMessage))
    assert count == 1


def test_distinct_message_ids_are_stored_separately(client: TestClient, session: Session) -> None:
    client.post("/messages", json=_payload(message_id="wamid.A"))
    client.post("/messages", json=_payload(message_id="wamid.B"))

    count = session.scalar(select(func.count()).select_from(InboundMessage))
    assert count == 2
