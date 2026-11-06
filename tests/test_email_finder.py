"""Tests for the pattern-first email pipeline."""

import os
import tempfile

import pytest
import pytest_asyncio

from sellbird.state import StateManager
from sellbird.integrations import email_finder as ef
from sellbird.integrations.email_finder import (
    EmailResult,
    build_email,
    classify_mx,
    derive_pattern,
    find_email,
    generate_patterns,
    infer_pattern_from_email,
    _status_from_verdict,
    _translate_hunter_pattern,
)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class Env:
    def __init__(self, **kw):
        self.reoon_api_key = kw.get("reoon", "")
        self.zerobounce_api_key = kw.get("zerobounce", "")


# ── pattern building / inference ──


def test_build_email_patterns():
    assert build_email("{f}.{l}", "Jane", "Doe", "acme.com") == "jane.doe@acme.com"
    assert build_email("{fi}{l}", "Jane", "Doe", "acme.com") == "jdoe@acme.com"
    assert build_email("{f}", "Jane", "Doe", "acme.com") == "jane@acme.com"
    assert build_email("{f}.{l}", "", "Doe", "acme.com") == ""
    assert build_email("nonexistent", "Jane", "Doe", "acme.com") == ""


def test_generate_patterns_dedup_and_order():
    patterns = generate_patterns("Jane", "Doe", "acme.com")
    assert patterns[0] == "jane.doe@acme.com"   # most common first
    assert "jdoe@acme.com" in patterns
    assert len(patterns) == len(set(patterns))


def test_infer_pattern_from_email():
    assert infer_pattern_from_email("jane.doe@acme.com", "Jane", "Doe") == "{f}.{l}"
    assert infer_pattern_from_email("jdoe@acme.com", "Jane", "Doe") == "{fi}{l}"
    assert infer_pattern_from_email("janedoe@acme.com", "Jane", "Doe") == "{f}{l}"
    assert infer_pattern_from_email("random@acme.com", "Jane", "Doe") is None


def test_translate_hunter_pattern():
    assert _translate_hunter_pattern("{first}.{last}") == "{f}.{l}"
    assert _translate_hunter_pattern("{f}{last}") == "{fi}{l}"
    assert _translate_hunter_pattern("{unknown}") is None


# ── MX classification ──


def test_classify_mx():
    assert classify_mx("aspmx.l.google.com") == "google"
    assert classify_mx("acme-com.mail.protection.outlook.com") == "microsoft"
    assert classify_mx("mx.pphosted.com") == "gateway"
    assert classify_mx("mail.smallco.com") == "other"
    assert classify_mx("") == "none"


# ── verdict → status mapping ──


def test_status_from_verdict():
    assert _status_from_verdict({"status": "valid", "catch_all": False}) == "verified"
    assert _status_from_verdict({"status": "deliverable"}) == "verified"
    assert _status_from_verdict({"status": "invalid"}) == "invalid"
    # catch_all always wins, even over a "valid" status
    assert _status_from_verdict({"status": "valid", "catch_all": True}) == "risky"
    assert _status_from_verdict({"status": "unknown"}) is None


# ── pattern derivation with cache + known emails ──


@pytest.mark.asyncio
async def test_derive_pattern_from_known_email(state):
    pattern, source, conf = await derive_pattern(
        "acme.com",
        known_emails=[("john.smith@acme.com", "John", "Smith")],
        state=state,
    )
    assert pattern == "{f}.{l}"
    assert source == "known_email"
    assert conf >= 0.9


@pytest.mark.asyncio
async def test_derive_pattern_uses_cache(state):
    await state.save_email_pattern(
        "acme.com", pattern="{fi}{l}", source="scraped_mailto", confidence=0.85
    )
    pattern, source, conf = await derive_pattern("acme.com", state=state)
    assert pattern == "{fi}{l}"
    assert source == "scraped_mailto"


@pytest.mark.asyncio
async def test_derive_pattern_default_no_signal(state, monkeypatch):
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    pattern, source, conf = await derive_pattern("unknown-co.com", state=state)
    assert pattern == "{f}.{l}"
    assert source == "default"
    assert conf < 0.5


# ── find_email end-to-end (mocked network) ──


@pytest.mark.asyncio
async def test_find_email_no_mx_is_invalid(state, monkeypatch):
    async def no_mx(domain):
        return None
    monkeypatch.setattr(ef, "get_mx_host", no_mx)
    result = await find_email("Jane", "Doe", "acme.com", state=state)
    assert result.status == "invalid"
    assert result.mx_type == "none"


@pytest.mark.asyncio
async def test_find_email_verify_false_is_guess(state, monkeypatch):
    async def google_mx(domain):
        return "aspmx.l.google.com"
    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")

    result = await find_email("Jane", "Doe", "acme.com", verify=False, state=state)
    assert result.status == "guess"
    assert result.email == "jane.doe@acme.com"
    assert result.mx_type == "google"
    # Pattern still cached even without verification
    cached = await state.get_email_pattern("acme.com")
    assert cached["mx_type"] == "google"


@pytest.mark.asyncio
async def test_find_email_verified_via_reoon(state, monkeypatch):
    async def google_mx(domain):
        return "aspmx.l.google.com"
    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")

    async def fake_reoon(email, key):
        return {"status": "valid", "catch_all": False}
    monkeypatch.setattr(ef, "verify_reoon", fake_reoon)

    result = await find_email(
        "Jane", "Doe", "acme.com", env=Env(reoon="k"), state=state
    )
    assert result.status == "verified"
    assert result.verified and result.sendable


@pytest.mark.asyncio
async def test_find_email_catch_all_is_risky(state, monkeypatch):
    async def ms_mx(domain):
        return "acme.mail.protection.outlook.com"
    monkeypatch.setattr(ef, "get_mx_host", ms_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")

    async def fake_zb(email, key):
        return {"status": "valid", "catch_all": True}
    monkeypatch.setattr(ef, "verify_zerobounce", fake_zb)

    result = await find_email(
        "Jane", "Doe", "acme.com", env=Env(zerobounce="k"), state=state
    )
    assert result.status == "risky"
    assert not result.sendable   # verified-only by default
    # catch-all flag persisted
    cached = await state.get_email_pattern("acme.com")
    assert cached["is_catch_all"] == 1


@pytest.mark.asyncio
async def test_find_email_invalid_from_provider(state, monkeypatch):
    async def google_mx(domain):
        return "aspmx.l.google.com"
    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")

    async def fake_reoon(email, key):
        return {"status": "invalid", "catch_all": False}
    monkeypatch.setattr(ef, "verify_reoon", fake_reoon)

    result = await find_email(
        "Jane", "Doe", "acme.com", env=Env(reoon="k"), state=state
    )
    assert result.status == "invalid"


@pytest.mark.asyncio
async def test_find_email_inconclusive_is_guess_not_verified(state, monkeypatch):
    """The core bug fix: no verifier confirmation → 'guess', never 'verified'."""
    async def google_mx(domain):
        return "aspmx.l.google.com"
    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    # No verifier keys configured at all → nothing can confirm.
    result = await find_email("Jane", "Doe", "acme.com", env=Env(), state=state)
    assert result.status == "guess"
    assert not result.verified


# ── shared Hunter domain-search budget ──


@pytest.mark.asyncio
async def test_hunter_budget_defaults_to_free_tier(state, monkeypatch):
    monkeypatch.delenv("HUNTER_MONTHLY_SEARCHES", raising=False)
    assert await ef.hunter_searches_remaining(state) == 25
    await ef.record_hunter_search(state)
    assert await ef.hunter_searches_remaining(state) == 24


@pytest.mark.asyncio
async def test_hunter_budget_follows_paid_plan_setting(state, monkeypatch):
    monkeypatch.setenv("HUNTER_MONTHLY_SEARCHES", "500")
    assert await ef.hunter_searches_remaining(state) == 500


@pytest.mark.asyncio
async def test_derive_pattern_spends_and_respects_hunter_budget(state, monkeypatch):
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "k")
    monkeypatch.setenv("HUNTER_MONTHLY_SEARCHES", "1")
    calls = []

    async def fake_hunter(domain, key):
        calls.append(domain)
        return "{f}.{l}", 0.8

    monkeypatch.setattr(ef, "_hunter_domain_pattern", fake_hunter)
    _, source, _ = await derive_pattern("first-co.com", state=state)
    assert source == "hunter_domain_search"
    _, source, _ = await derive_pattern("second-co.com", state=state)
    assert source == "default"
    assert calls == ["first-co.com"]
