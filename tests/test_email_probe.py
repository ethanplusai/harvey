"""Tests for the keyless control-checked recipient probe.

The probe is what lets a host with no verifier key (no Reoon/ZeroBounce/
Hunter) turn a pattern guess into a real ``verified`` / ``risky`` / ``invalid``
verdict: it RCPTs a guaranteed-fake control address and the candidate in one
session and only trusts an answer the control supports.

Every test here stubs SMTP — nothing leaves the machine (see conftest.py,
which also neutralises the probe for all other tests).
"""

import os
import tempfile

import pytest
import pytest_asyncio

from harvey.state import StateManager
from harvey.integrations import email_finder as ef

# The real probe, captured before conftest's autouse stub replaces the
# module attribute for every test.
REAL_PROBE = ef.probe_recipient


class FakeResponse:
    def __init__(self, code):
        self.code = code
        self.message = b""


class FakeSMTP:
    """Stands in for aiosmtplib.SMTP; `codes` maps address -> SMTP reply code."""

    last_instance = None

    def __init__(self, codes, fail_connect=False):
        self.codes = codes
        self.fail_connect = fail_connect
        self.sent = []
        FakeSMTP.last_instance = self

    async def connect(self):
        if self.fail_connect:
            raise OSError("tarpit")
        return FakeResponse(220)

    async def ehlo(self, name=None):
        return FakeResponse(250)

    async def mail(self, sender, options=None):
        self.sent.append(("MAIL", sender))
        return FakeResponse(250)

    async def rcpt(self, addr, options=None):
        self.sent.append(("RCPT", addr))
        code = self.codes.get(addr, self.codes.get("*", 550))
        if code != 250:
            raise ef.aiosmtplib.SMTPRecipientRefused(code, b"no", addr)
        return FakeResponse(code)

    async def rset(self):
        return FakeResponse(250)

    async def quit(self):
        return FakeResponse(221)

    async def close(self):
        return None


class Env:
    def __init__(self, reoon="", zerobounce="", smtp_probe=True):
        self.reoon_api_key = reoon
        self.zerobounce_api_key = zerobounce
        self.smtp_probe = smtp_probe


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.fixture(autouse=True)
def _clean_probe_state():
    """Cached verdicts and the probe budget are process globals; isolate them."""
    ef.reset_probe_state()
    yield
    ef.reset_probe_state()


def _use_smtp(monkeypatch, codes, fail_connect=False):
    def factory(*args, **kwargs):
        return FakeSMTP(codes, fail_connect=fail_connect)

    monkeypatch.setattr(ef.aiosmtplib, "SMTP", factory)

    async def mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", mx)


# ── verdict mapping ──


@pytest.mark.asyncio
async def test_probe_valid_when_control_rejected(monkeypatch):
    _use_smtp(monkeypatch, {"jane.doe@acme.com": 250, "*": 550})
    verdict = await REAL_PROBE("jane.doe@acme.com")
    assert verdict["status"] == "valid"
    assert verdict["catch_all"] is False
    assert verdict["evidence"] == {"control": 550, "candidate": 250}
    # Control was asked first, so a catch-all can never be called valid.
    sent = FakeSMTP.last_instance.sent
    assert sent[0][0] == "MAIL"
    assert sent[1][1].startswith("zz-no-such-user-")
    assert sent[2][1] == "jane.doe@acme.com"


@pytest.mark.asyncio
async def test_probe_catch_all_is_never_valid(monkeypatch):
    _use_smtp(monkeypatch, {"*": 250})
    verdict = await REAL_PROBE("jane.doe@acme.com")
    assert verdict["status"] == "catch_all"
    assert verdict["catch_all"] is True


@pytest.mark.asyncio
async def test_probe_invalid_when_both_rejected(monkeypatch):
    _use_smtp(monkeypatch, {"*": 550})
    verdict = await REAL_PROBE("nobody@acme.com")
    assert verdict["status"] == "invalid"


@pytest.mark.asyncio
async def test_probe_inconclusive_on_4xx(monkeypatch):
    _use_smtp(monkeypatch, {"*": 450})
    assert await REAL_PROBE("jane@acme.com") is None


@pytest.mark.asyncio
async def test_probe_inconclusive_when_connection_fails(monkeypatch):
    _use_smtp(monkeypatch, {"*": 250}, fail_connect=True)
    assert await REAL_PROBE("jane@acme.com") is None


@pytest.mark.asyncio
async def test_probe_budget_caps_network_calls(monkeypatch):
    monkeypatch.setattr(ef, "_probe_budget", {"left": 1})
    _use_smtp(monkeypatch, {"*": 550})
    assert await REAL_PROBE("jane@acme.com") is not None
    assert await REAL_PROBE("mark@acme.com") is None      # budget spent -> inconclusive


@pytest.mark.asyncio
async def test_probe_caches_per_address(monkeypatch):
    _use_smtp(monkeypatch, {"*": 550})
    first = await REAL_PROBE("jane@acme.com")
    second = await REAL_PROBE("jane@acme.com")
    assert first == second
    assert len([s for s in FakeSMTP.last_instance.sent if s[0] == "MAIL"]) == 1


# ── wiring into verification + the CLI backfill path ──


@pytest.mark.asyncio
async def test_find_email_turns_guess_into_verified_without_keys(state, monkeypatch):
    """No keys at all: the probe is the last resort and may say 'verified'."""
    async def google_mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")

    async def fake_probe(email):
        return {"status": "valid", "catch_all": False,
                "evidence": {"control": 550, "candidate": 250}}

    monkeypatch.setattr(ef, "probe_recipient", fake_probe)

    result = await ef.find_email("Jane", "Doe", "acme.com", env=Env(), state=state)
    assert result.status == "verified"
    assert result.sendable


@pytest.mark.asyncio
async def test_probe_not_used_when_a_key_is_configured(state, monkeypatch):
    """A paid verifier's silence stays 'guess' — no cheating with port 25."""
    async def google_mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    calls = []

    async def fake_probe(email):
        calls.append(email)
        return {"status": "valid", "catch_all": False, "evidence": {}}

    monkeypatch.setattr(ef, "probe_recipient", fake_probe)

    async def silent_reoon(email, key):
        return None

    monkeypatch.setattr(ef, "verify_reoon", silent_reoon)

    result = await ef.find_email(
        "Jane", "Doe", "acme.com", env=Env(reoon="k"), state=state
    )
    assert result.status == "guess"
    assert calls == []


@pytest.mark.asyncio
async def test_probe_respects_the_env_switch(state, monkeypatch):
    async def google_mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    monkeypatch.setattr(ef, "probe_recipient", REAL_PROBE)
    _use_smtp(monkeypatch, {"*": 550})

    result = await ef.find_email(
        "Jane", "Doe", "acme.com", env=Env(smtp_probe=False), state=state
    )
    assert result.status == "guess"


@pytest.mark.asyncio
async def test_verify_address_regrades_a_stored_guess(state, monkeypatch):
    async def google_mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    monkeypatch.setattr(ef, "probe_recipient", REAL_PROBE)
    _use_smtp(monkeypatch, {"jane.doe@acme.com": 250, "*": 550})

    result = await ef.verify_address("jane.doe@acme.com", env=Env(), state=state)
    assert result.status == "verified"
    assert result.mx_type == "google"
    assert ef.last_probe_evidence("jane.doe@acme.com")["status"] == "valid"


@pytest.mark.asyncio
async def test_state_backfill_query_selects_only_unconfirmed(state):
    from harvey.models.prospect import Prospect

    await state.add_prospect(Prospect(
        first_name="Guess", last_name="Only", email="guess.only@acme.com",
        email_status="guess", company="Acme", status="new",
    ))
    await state.add_prospect(Prospect(
        first_name="Done", last_name="Already", email="done@acme.com",
        email_status="verified", email_verified=True, company="Acme", status="new",
    ))
    rows = await state.get_prospects_by_email_status(("", "guess"))
    assert [p.email for p in rows] == ["guess.only@acme.com"]
    assert await state.get_prospects_by_email_status(("", "guess"), domain="other.com") == []
    assert await state.get_prospects_by_email_status(
        ("", "guess"), domain="acme.com", limit=1)
