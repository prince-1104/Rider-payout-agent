"""Provider contract, failure handling, and payment safety without real payouts."""

import json
from typing import Callable

import httpx
import pytest

from app.config import get_settings
from app.integrations.payswift import (
    PaySwiftClient,
    PaySwiftClientError,
    PaySwiftProtocolError,
    PaySwiftRateLimitError,
    PaySwiftServerError,
    PaySwiftTimeoutError,
    PaySwiftTransportError,
)


@pytest.fixture(autouse=True)
def provider_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PAYSWIFT_BASE_URL", "https://payswift.test/")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> PaySwiftClient:
    return PaySwiftClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("body", [[], [{"payout_id": "P1"}], {"items": []}])
def test_list_payouts_uses_configured_url_and_rider_filter(body):
    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "payswift.test"
        assert request.url.path == "/v1/payouts"
        assert dict(request.url.params) == {"rider_id": "R017"}
        assert request.headers["Accept"] == "application/json"
        assert request.extensions["timeout"] == {
            "connect": 2.0, "read": 5.0, "write": 5.0, "pool": 2.0
        }
        return httpx.Response(200, json=body)

    with make_client(handler) as client:
        assert client.list_payouts("R017") == body


@pytest.mark.parametrize("amount", [1, 120, 201, 10000])
def test_create_sends_exact_authorized_amount_and_required_key(amount):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/payouts"
        assert request.headers["Idempotency-Key"] == "payment-stable-key"
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == {
            "rider_id": "R017", "amount": amount, "reference": "authorized-dispute"
        }
        return httpx.Response(201, json={"payout_id": "P1", "status": "processed"})

    with make_client(handler) as client:
        assert client.create_payout("R017", amount, "authorized-dispute", "payment-stable-key") == {
            "payout_id": "P1", "status": "processed"
        }


@pytest.mark.parametrize("amount", [0, -1, 10001, True, False, 1.5, "120", None])
def test_invalid_amount_never_reaches_provider(amount):
    def handler(request):
        pytest.fail("Invalid payment must be rejected before HTTP")

    with make_client(handler) as client, pytest.raises(ValueError, match="amount"):
        client.create_payout("R017", amount, "dispute", "key")


@pytest.mark.parametrize(
    "field,value",
    [("rider_id", ""), ("rider_id", None), ("reference", " "),
     ("idempotency_key", ""), ("idempotency_key", None),
     ("idempotency_key", "bad\r\nkey"), ("idempotency_key", "nonascii-₹")],
)
def test_invalid_create_fields_never_reach_provider(field, value):
    def handler(request):
        pytest.fail("Invalid payment must be rejected before HTTP")

    arguments = {"rider_id": "R017", "amount": 120, "reference": "dispute", "idempotency_key": "key"}
    arguments[field] = value
    with make_client(handler) as client, pytest.raises(ValueError, match=field):
        client.create_payout(**arguments)


@pytest.mark.parametrize("rider_id", ["", " ", None])
def test_invalid_list_rider_never_reaches_provider(rider_id):
    with make_client(lambda request: pytest.fail("Unexpected HTTP")) as client:
        with pytest.raises(ValueError, match="rider_id"):
            client.list_payouts(rider_id)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "status,error_type",
    [(400, PaySwiftClientError), (401, PaySwiftClientError),
     (403, PaySwiftClientError), (404, PaySwiftClientError),
     (409, PaySwiftClientError), (422, PaySwiftClientError),
     (429, PaySwiftRateLimitError), (500, PaySwiftServerError),
     (502, PaySwiftServerError), (503, PaySwiftServerError)],
)
def test_http_errors_are_classified_and_never_automatically_retried(method, status, error_type):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "sensitive-provider-body"}, headers={"Retry-After": "3"})

    with make_client(handler) as client, pytest.raises(error_type) as caught:
        if method == "GET":
            client.list_payouts("R017")
        else:
            client.create_payout("R017", 120, "dispute", "stable-key")
    assert len(calls) == 1
    assert caught.value.status_code == status
    assert caught.value.retry_after == "3"
    assert "sensitive-provider-body" not in str(caught.value)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "exception,error_type",
    [(httpx.ReadTimeout, PaySwiftTimeoutError), (httpx.ConnectTimeout, PaySwiftTimeoutError),
     (httpx.PoolTimeout, PaySwiftTimeoutError), (httpx.WriteTimeout, PaySwiftTimeoutError),
     (httpx.ConnectError, PaySwiftTransportError), (httpx.ReadError, PaySwiftTransportError)],
)
def test_transport_failures_are_not_retried(method, exception, error_type):
    calls = []

    def handler(request):
        calls.append(request)
        raise exception("simulated", request=request)

    with make_client(handler) as client, pytest.raises(error_type):
        if method == "GET":
            client.list_payouts("R017")
        else:
            client.create_payout("R017", 120, "dispute", "stable-key")
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_redirects_are_not_followed(method):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://other.test/v1/payouts"})

    with make_client(handler) as client, pytest.raises(PaySwiftProtocolError):
        if method == "GET":
            client.list_payouts("R017")
        else:
            client.create_payout("R017", 120, "dispute", "key")
    assert len(calls) == 1


@pytest.mark.parametrize("response", [httpx.Response(200, text="not json"), httpx.Response(204),
                                      httpx.Response(200, json=None), httpx.Response(200, json=[1])])
def test_malformed_success_response_is_reported(response):
    with make_client(lambda request: response) as client, pytest.raises(PaySwiftProtocolError):
        client.list_payouts("R017")


@pytest.mark.parametrize("body", [[], {}, {"payout_id": "P1"}, {"status": "processed"},
                                  {"payout_id": "", "status": "processed"},
                                  {"payout_id": "P1", "status": 123}])
def test_malformed_payment_acknowledgement_is_reported(body):
    with make_client(lambda request: httpx.Response(201, json=body)) as client:
        with pytest.raises(PaySwiftProtocolError):
            client.create_payout("R017", 120, "dispute", "key")


def test_explicit_retry_preserves_key_and_amount():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("unknown outcome", request=request)
        return httpx.Response(200, json={"payout_id": "P1", "status": "processed"})

    with make_client(handler) as client:
        with pytest.raises(PaySwiftTimeoutError):
            client.create_payout("R017", 120, "dispute", "stable-key")
        assert len(calls) == 1
        # Caller chooses a retry after reconciliation; no key or amount is generated by the client.
        client.create_payout("R017", 120, "dispute", "stable-key")
    assert calls[0].content == calls[1].content
    assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]


@pytest.mark.parametrize("url", ["ftp://payswift.test", "http:///", "https://user:password@payswift.test",
                                 "https://payswift.test?token=secret", "https://payswift.test#fragment"])
def test_invalid_base_url_is_rejected(monkeypatch, url):
    monkeypatch.setenv("PAYSWIFT_BASE_URL", url)
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        PaySwiftClient()


def test_client_closes_transport():
    class Transport(httpx.MockTransport):
        closed = False

        def close(self):
            self.closed = True

    transport = Transport(lambda request: httpx.Response(200, json=[]))
    with PaySwiftClient(transport=transport) as client:
        client.list_payouts("R017")
    assert transport.closed
    client.close()
