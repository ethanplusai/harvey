"""Verifier API keys never reach the terminal.

httpx logs every request at INFO with its full URL. Reoon and ZeroBounce only
accept their key as a query parameter, so at INFO `sellbird run` printed the
keys in clear text on every verification. Hunter accepts a header instead.
Two fixes: Hunter's key travels in X-API-KEY, and sellbird.main holds the
httpx/httpcore loggers at WARNING so no request URL is echoed at all.
"""

import logging

import httpx
import pytest

import sellbird.integrations.email_finder as ef
import sellbird.main  # noqa: F401  (configures logging, including the httpx loggers)

KEY = "sk-test-FAKEKEY-0123456789"


class _Transport(httpx.AsyncBaseTransport):
    """Answers every request with a verifier-style JSON body and records it."""

    def __init__(self, body):
        self.body = body
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json=self.body, request=request)


def _mock_client(monkeypatch, body):
    transport = _Transport(body)
    real_client = httpx.AsyncClient

    def client(*a, **kw):
        kw.pop("transport", None)
        return real_client(transport=transport, **kw)

    monkeypatch.setattr(ef.httpx, "AsyncClient", client)
    return transport


def test_http_client_loggers_do_not_echo_request_urls():
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


@pytest.mark.asyncio
@pytest.mark.parametrize("verify, body", [
    (ef.verify_reoon, {"status": "safe", "is_catch_all": False}),
    (ef.verify_zerobounce, {"status": "valid", "sub_status": ""}),
    (ef.verify_hunter, {"data": {"status": "valid", "accept_all": False}}),
])
async def test_a_verifier_call_does_not_log_the_key(monkeypatch, caplog, verify, body):
    transport = _mock_client(monkeypatch, body)
    caplog.set_level(logging.DEBUG)  # root at DEBUG: anything any logger emits is caught
    result = await verify("pat@example.com", KEY)
    assert result and result["status"] in ("valid", "safe")
    assert len(transport.requests) == 1
    assert KEY not in caplog.text
    assert not [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))]


@pytest.mark.asyncio
async def test_hunter_sends_the_key_in_a_header_not_the_url(monkeypatch):
    transport = _mock_client(monkeypatch, {"data": {"status": "valid"}})
    await ef.verify_hunter("pat@example.com", KEY)
    transport.body = {"data": {"pattern": "{first}.{last}", "emails": []}}
    await ef._hunter_domain_pattern("example.com", KEY)
    await ef.hunter_domain_people("example.com", KEY)
    assert len(transport.requests) == 3
    for request in transport.requests:
        assert KEY not in str(request.url)
        assert request.headers.get("X-API-KEY") == KEY


@pytest.mark.asyncio
async def test_a_failed_request_is_logged_without_the_key(monkeypatch, caplog):
    async def boom(self, request):
        raise httpx.ConnectError(f"could not reach {request.url}", request=request)

    monkeypatch.setattr(_Transport, "handle_async_request", boom)
    _mock_client(monkeypatch, {})
    caplog.set_level(logging.DEBUG)
    assert await ef.verify_reoon("pat@example.com", KEY) is None
    assert await ef.verify_zerobounce("pat@example.com", KEY) is None
    assert "failed" in caplog.text
    assert KEY not in caplog.text
