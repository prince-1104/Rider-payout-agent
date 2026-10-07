"""Opt-in, read-only contract check against a running PaySwift sandbox.

PowerShell:
    $env:RUN_PAYSWIFT_INTEGRATION='1'; python -m pytest tests/test_payswift_integration.py -q
Set PAYSWIFT_BASE_URL if the sandbox is not at http://localhost:8081.
No real payments are issued by this test.
"""

import os

import httpx
import pytest

from app.config import get_settings
from app.integrations.payswift import PaySwiftClient


@pytest.mark.skipif(os.getenv("RUN_PAYSWIFT_INTEGRATION") != "1", reason="Set RUN_PAYSWIFT_INTEGRATION=1 for live sandbox")
def test_live_payswift_ledger():
    get_settings.cache_clear()
    try:
        base_url = get_settings().payswift_base_url
        try:
            health = httpx.get(f"{base_url}/health", timeout=2.0)
        except httpx.RequestError:
            pytest.skip("PaySwift sandbox is unavailable")
        assert health.status_code == 200, "PaySwift is reachable but unhealthy"
        with PaySwiftClient() as client:
            result = client.list_payouts("R017")
        assert isinstance(result, (dict, list))
    finally:
        get_settings.cache_clear()
