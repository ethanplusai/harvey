"""Gmail meters calls per user per minute: a quota answer is retried with
backoff instead of failing the call (and with it the whole read, which
then holds every send that cycle)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from sellbird.integrations import gmail
from sellbird.integrations.gmail import RETRY_DELAYS, GmailProvider

QUOTA_403 = json.dumps({"error": {"code": 403, "message": (
    "Quota exceeded for quota metric 'Total Query Cost' and limit "
    "'Units per minute per user' of service 'gmail.googleapis.com'")}})
PROFILE = json.dumps({"emailAddress": "sender@example.com"})


def provider(answers, monkeypatch):
    """A provider whose transport pops one canned answer per request."""
    calls = []
    slept = []

    def handler(request):
        calls.append(request.url.path)
        status, text, headers = answers.pop(0)
        return httpx.Response(status, text=text, headers=headers)

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gmail, "_sleep", fake_sleep)
    p = GmailProvider(SimpleNamespace(), SimpleNamespace(gmail_client_id="id", gmail_client_secret="s"),
                      transport=httpx.MockTransport(handler))
    p._token = AsyncMock(return_value="synthetic-token")
    p._load = lambda: {"refresh_token": "synthetic"}
    return p, calls, slept


@pytest.mark.asyncio
async def test_a_quota_403_is_retried_with_backoff(monkeypatch):
    p, calls, slept = provider([(403, QUOTA_403, {}), (403, QUOTA_403, {}), (200, PROFILE, {})], monkeypatch)
    ok, detail = await p.test_connection()
    assert ok and "sender@example.com" in detail
    assert len(calls) == 3 and slept == [2.0, 4.0]


@pytest.mark.asyncio
async def test_a_429_honours_retry_after(monkeypatch):
    p, calls, slept = provider([(429, "slow down", {"Retry-After": "7"}), (200, PROFILE, {})], monkeypatch)
    ok, _ = await p.test_connection()
    assert ok and slept == [7.0]


@pytest.mark.asyncio
async def test_other_403s_are_not_retried(monkeypatch):
    p, calls, slept = provider([(403, json.dumps({"error": {"message": "insufficientPermissions"}}), {})], monkeypatch)
    ok, _ = await p.test_connection()
    assert not ok and len(calls) == 1 and slept == []


@pytest.mark.asyncio
async def test_a_quota_that_never_lifts_fails_after_the_last_delay(monkeypatch):
    answers = [(429, "", {})] * (len(RETRY_DELAYS) + 1)
    p, calls, slept = provider(answers, monkeypatch)
    ok, _ = await p.test_connection()
    assert not ok and len(calls) == len(RETRY_DELAYS) + 1 and slept == list(RETRY_DELAYS)
