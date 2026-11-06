"""EmailVerify.io: an optional verifier, used when EMAILVERIFY_API_KEY is set."""

import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey.integrations import email_finder as ef
from harvey.integrations.email_finder import _status_from_verdict, find_email


@pytest_asyncio.fixture
async def state():
    from harvey.state import StateManager
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class Env:
    def __init__(self, **kw):
        self.emailverify_api_key = kw.get("emailverify", "")
        self.reoon_api_key = kw.get("reoon", "")
        self.zerobounce_api_key = kw.get("zerobounce", "")


def _fake_api(monkeypatch, status_code=200, body=None, seen=None):
    """Route EmailVerify requests to a canned answer."""
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status_code, json=body if body is not None else {})

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(ef.httpx, "AsyncClient", client)


# ── response mapping ──

@pytest.mark.asyncio
@pytest.mark.parametrize("status,sub,expected", [
    ("valid", "permitted", "verified"),
    ("invalid", "mailbox_not_found", "invalid"),
    ("catch_all", "none", "risky"),
    ("do_not_mail", "disposable", "invalid"),
    ("do_not_mail", "mailbox_quota_exceeded", "invalid"),
    ("role_based", "permitted", "verified"),
    ("role_based", "none", None),
    ("unknown", "none", None),
    ("skipped", "none", None),
])
async def test_status_mapping(monkeypatch, status, sub, expected):
    _fake_api(monkeypatch, body={"email": "a@acme.test", "status": status, "sub_status": sub})
    verdict = await ef.verify_emailverify("a@acme.test", "k")
    assert _status_from_verdict(verdict) == expected


@pytest.mark.asyncio
async def test_request_shape(monkeypatch):
    seen = []
    _fake_api(monkeypatch, body={"status": "valid", "sub_status": "permitted"}, seen=seen)
    await ef.verify_emailverify("jane@acme.test", "secret-key")
    req = seen[0]
    assert req.method == "GET"
    assert str(req.url).startswith(ef.EMAILVERIFY_URL)
    assert req.url.params["key"] == "secret-key"
    assert req.url.params["email"] == "jane@acme.test"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 402, 429, 500])
async def test_api_errors_are_unavailable(monkeypatch, code):
    _fake_api(monkeypatch, status_code=code, body={"error": "nope"})
    assert await ef.verify_emailverify("a@acme.test", "k") is None


@pytest.mark.asyncio
async def test_network_error_is_unavailable(monkeypatch):
    real_client = httpx.AsyncClient

    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(ef.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(boom)}))
    assert await ef.verify_emailverify("a@acme.test", "k") is None


# ── where it sits in the order ──

def _mx(monkeypatch, host="aspmx.l.google.com"):
    async def fake_mx(domain):
        return host
    monkeypatch.setattr(ef, "get_mx_host", fake_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")


def _verdicts(monkeypatch, calls, **answers):
    """Fake each named verifier; record the order they're called in."""
    for name, answer in answers.items():
        async def fake(email, key, name=name, answer=answer):
            calls.append(name)
            return answer
        monkeypatch.setattr(ef, f"verify_{name}", fake)


VALID = {"status": "valid", "catch_all": False}
UNKNOWN = {"status": "unknown", "catch_all": False}


@pytest.mark.asyncio
async def test_existing_verifiers_still_go_first(state, monkeypatch):
    _mx(monkeypatch)
    calls = []
    _verdicts(monkeypatch, calls, zerobounce=VALID, reoon=VALID, emailverify=VALID)
    result = await find_email("Jane", "Doe", "acme.com", state=state,
                              env=Env(emailverify="k", reoon="k", zerobounce="k"))
    assert result.status == "verified"
    assert calls == ["zerobounce"]


@pytest.mark.asyncio
async def test_emailverify_settles_what_reoon_cannot(state, monkeypatch):
    _mx(monkeypatch)
    calls = []
    _verdicts(monkeypatch, calls, reoon=UNKNOWN, emailverify=VALID)
    result = await find_email("Jane", "Doe", "acme.com", state=state,
                              env=Env(emailverify="k", reoon="k"))
    assert result.status == "verified"
    assert calls == ["reoon", "emailverify"]


@pytest.mark.asyncio
async def test_emailverify_alone_is_enough(state, monkeypatch):
    _mx(monkeypatch)
    calls = []
    _verdicts(monkeypatch, calls, emailverify={"status": "invalid", "catch_all": False})
    result = await find_email("Jane", "Doe", "acme.com", state=state, env=Env(emailverify="k"))
    assert result.status == "invalid"
    assert calls == ["emailverify"]


@pytest.mark.asyncio
async def test_catch_all_stays_risky(state, monkeypatch):
    _mx(monkeypatch)
    _verdicts(monkeypatch, [], emailverify={"status": "catch_all", "catch_all": True})
    result = await find_email("Jane", "Doe", "acme.com", state=state, env=Env(emailverify="k"))
    assert result.status == "risky"
    assert (await state.get_email_pattern("acme.com"))["is_catch_all"] == 1


@pytest.mark.asyncio
async def test_small_domain_asks_emailverify_before_smtp(state, monkeypatch):
    _mx(monkeypatch, host="mail.acme.com")

    async def no_smtp(email):
        raise AssertionError("SMTP probed although EmailVerify.io decided")
    monkeypatch.setattr(ef, "verify_email_smtp", no_smtp)
    calls = []
    _verdicts(monkeypatch, calls, reoon=UNKNOWN, emailverify=VALID)
    result = await find_email("Jane", "Doe", "acme.com", state=state,
                              env=Env(emailverify="k", reoon="k"))
    assert result.status == "verified"
    assert calls == ["reoon", "emailverify"]


@pytest.mark.asyncio
async def test_no_key_never_calls_it(state, monkeypatch):
    _mx(monkeypatch)

    async def fake_ev(email, key):
        raise AssertionError("EmailVerify called without a key")
    monkeypatch.setattr(ef, "verify_emailverify", fake_ev)
    _verdicts(monkeypatch, [], reoon={"status": "safe", "catch_all": False})
    result = await find_email("Jane", "Doe", "acme.com", state=state, env=Env(reoon="k"))
    assert result.status == "verified"
