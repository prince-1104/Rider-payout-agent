"""Bounded, single-attempt HTTP access to the PaySwift payment provider.

The caller must authorize ``amount`` in the domain layer before calling this
client. It is sent unchanged: this module never calculates, increases, or
authorizes a payout. A message ID alone is not a payment idempotency key; callers
must persist a stable key for the *same authorized payment* across retries.
"""

from typing import Any

import httpx

from app.config import get_settings


class PaySwiftError(Exception):
    """Base class for provider failures; response bodies are deliberately omitted."""


class PaySwiftHTTPError(PaySwiftError):
    """An unsuccessful HTTP response, with metadata needed for reconciliation."""

    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"PaySwift returned HTTP {status_code}")


class PaySwiftClientError(PaySwiftHTTPError):
    """4xx rejection (including conflicts); do not blindly retry it."""


class PaySwiftRateLimitError(PaySwiftHTTPError):
    """429 response. Retry-After is exposed, but the client does not sleep/retry."""


class PaySwiftServerError(PaySwiftHTTPError):
    """5xx response. A submitted payment may have succeeded; reconcile first."""


class PaySwiftTransportError(PaySwiftError):
    """Network failure; the outcome of a POST may be unknown."""


class PaySwiftTimeoutError(PaySwiftTransportError):
    """Request timeout; never assume a timed-out POST failed to move money."""


class PaySwiftProtocolError(PaySwiftError):
    """Unexpected response format/status; a POST outcome may be unknown."""


class PaySwiftClient:
    """Synchronous client with connection reuse and no automatic retries.

    ``PAYSWIFT_BASE_URL`` is read via application settings. An optional transport
    supports mocked HTTP tests. Use as a context manager or call ``close()``.
    The default per-operation limits fit comfortably within the vendor's ~10s
    webhook deadline; callers must also budget their overall workflow duration.

    List responses are returned in the provider's native JSON shape (an object
    or list). Create responses contain at least ``payout_id`` and ``status``.
    """

    def __init__(
        self,
        *,
        timeout: httpx.Timeout | float = httpx.Timeout(
            connect=2.0, read=5.0, write=5.0, pool=2.0
        ),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = httpx.URL(get_settings().payswift_base_url)
        if (
            base_url.scheme not in {"http", "https"}
            or not base_url.host
            or base_url.userinfo
            or base_url.query
            or base_url.fragment
        ):
            raise ValueError("PAYSWIFT_BASE_URL must be an HTTP(S) URL without credentials, query, or fragment")
        self._client = httpx.Client(
            base_url=str(base_url).rstrip("/") + "/",
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> "PaySwiftClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release pooled connections. Safe to call more than once."""
        self._client.close()

    @staticmethod
    def _required_text(value: str, field: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")

    def list_payouts(self, rider_id: str) -> dict[str, Any] | list[dict[str, Any]]:
        """Read the provider ledger for exactly the supplied rider."""
        self._required_text(rider_id, "rider_id")
        return self._request("GET", params={"rider_id": rider_id})

    def create_payout(
        self,
        rider_id: str,
        amount: int,
        reference: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Send exactly the domain-authorized amount using a mandatory stable key.

        A return value confirms a valid provider acknowledgement, not settlement.
        On network/5xx/protocol failures, consult the ledger before deciding to
        retry. Any retry must reuse the same key and identical payment payload.
        """
        self._required_text(rider_id, "rider_id")
        # bool is an int subclass, but is not a valid whole-rupee authorization.
        if type(amount) is not int or not 1 <= amount <= 10000:
            raise ValueError("amount must be an authorized integer between 1 and 10000 rupees")
        self._required_text(reference, "reference")
        self._required_text(idempotency_key, "idempotency_key")
        if any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key):
            raise ValueError("idempotency_key must contain only visible ASCII characters")

        result = self._request(
            "POST",
            json={"rider_id": rider_id, "amount": amount, "reference": reference},
            headers={"Idempotency-Key": idempotency_key},
        )
        if not isinstance(result, dict) or any(
            not isinstance(result.get(field), str) or not result[field].strip()
            for field in ("payout_id", "status")
        ):
            raise PaySwiftProtocolError("PaySwift payout acknowledgement is missing payout_id or status")
        return result

    def _request(self, method: str, **kwargs: Any) -> dict[str, Any] | list[dict[str, Any]]:
        try:
            response = self._client.request(method, "v1/payouts", **kwargs)
        except httpx.TimeoutException as exc:
            raise PaySwiftTimeoutError("PaySwift request timed out; payment outcome may be unknown") from exc
        except httpx.RequestError as exc:
            raise PaySwiftTransportError("PaySwift request failed; payment outcome may be unknown") from exc

        code = response.status_code
        retry_after = response.headers.get("Retry-After")
        if code == 429:
            raise PaySwiftRateLimitError(code, retry_after)
        if 400 <= code < 500:
            raise PaySwiftClientError(code, retry_after)
        if 500 <= code < 600:
            raise PaySwiftServerError(code, retry_after)
        if not 200 <= code < 300:
            raise PaySwiftProtocolError(f"Unexpected PaySwift HTTP status {code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise PaySwiftProtocolError("PaySwift returned invalid JSON") from exc
        if not isinstance(result, dict) and not (
            isinstance(result, list) and all(isinstance(item, dict) for item in result)
        ):
            raise PaySwiftProtocolError("PaySwift returned an unexpected JSON shape")
        return result
