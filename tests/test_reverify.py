"""The second look: guessed addresses get verified, and the budget holds.

A contact found before a verifier key existed stays a guess forever. The
re-verify stage is what looks at it again — and what keeps the free tier's
daily credits from being spent in one afternoon.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.models.prospect import Prospect
from sellbird.reverify import (
    DAILY_BUDGET, record_spend, reverify_batch, spent_today,
)
from sellbird.state import StateManager


def env(**kw):
    base = dict(reoon_api_key="", zerobounce_api_key="", hunter_api_key="")
    return SimpleNamespace(**{**base, **kw})


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(Path(tmp_path) / "reverify.db"))
    await s.init_db()
    return s


async def add(state, email, email_status="guess", score=0):
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="Owner", company="Ana Co",
        industry="Manufacturing", email=email, email_status=email_status,
        score=score))
    return pid


@pytest.fixture
def verifier(monkeypatch):
    """Stub the verifier: MX resolves, and the verdict is whatever a test says."""
    calls = []

    def setup(status, catch_all=False):
        async def fake_mx(domain):
            return "mx." + domain

        async def fake_verify(email, mx_type, env):
            calls.append(email)
            return status, catch_all

        monkeypatch.setattr("sellbird.integrations.email_finder.get_mx_host", fake_mx)
        monkeypatch.setattr("sellbird.integrations.email_finder._verify_candidate", fake_verify)
        return calls

    return setup


@pytest.mark.asyncio
async def test_no_verifier_key_changes_nothing(state, verifier):
    verifier("verified")
    pid = await add(state, "ana@acme.test")
    report = await reverify_batch(state, env())
    assert "skipped" in report
    assert report["checked"] == 0
    prospect = await state.get_prospect(pid)
    assert prospect.email_status == "guess"


@pytest.mark.asyncio
async def test_a_safe_address_becomes_verified(state, verifier):
    calls = verifier("verified")
    pid = await add(state, "ana@acme.test")
    report = await reverify_batch(state, env(hunter_api_key="k"))
    assert calls == ["ana@acme.test"]
    assert report["verified"] == 1
    prospect = await state.get_prospect(pid)
    assert prospect.email_status == "verified"
    assert prospect.email_verified is True


@pytest.mark.asyncio
async def test_an_invalid_address_becomes_invalid(state, verifier):
    verifier("invalid")
    pid = await add(state, "ana@acme.test")
    report = await reverify_batch(state, env(hunter_api_key="k"))
    assert report["invalid"] == 1
    assert (await state.get_prospect(pid)).email_status == "invalid"


@pytest.mark.asyncio
async def test_a_catch_all_is_risky_not_verified(state, verifier):
    verifier("risky", True)
    pid = await add(state, "ana@acme.test")
    report = await reverify_batch(state, env(hunter_api_key="k"))
    assert report["risky"] == 1
    assert (await state.get_prospect(pid)).email_status == "risky"


@pytest.mark.asyncio
async def test_no_verdict_leaves_the_guess_alone(state, verifier):
    verifier(None)
    pid = await add(state, "ana@acme.test")
    report = await reverify_batch(state, env(hunter_api_key="k"))
    assert report["undecided"] == 1
    assert (await state.get_prospect(pid)).email_status == "guess"


@pytest.mark.asyncio
async def test_a_domain_without_mx_is_invalid(state, monkeypatch):
    async def no_mx(domain):
        return None

    monkeypatch.setattr("sellbird.integrations.email_finder.get_mx_host", no_mx)
    pid = await add(state, "ana@acme.test")
    report = await reverify_batch(state, env(hunter_api_key="k"))
    assert report["invalid"] == 1
    assert (await state.get_prospect(pid)).email_status == "invalid"


@pytest.mark.asyncio
async def test_only_guesses_are_looked_at(state, verifier):
    calls = verifier("verified")
    await add(state, "kept@acme.test", email_status="verified")
    await add(state, "gone@acme.test", email_status="invalid")
    await add(state, "todo@acme.test", email_status="guess")
    await reverify_batch(state, env(hunter_api_key="k"))
    assert calls == ["todo@acme.test"]


@pytest.mark.asyncio
async def test_the_days_budget_is_never_exceeded(state, verifier):
    verifier("verified")
    await record_spend(state, DAILY_BUDGET)
    report = await reverify_batch(state, env(hunter_api_key="k"))
    assert "skipped" in report
    assert report["checked"] == 0


@pytest.mark.asyncio
async def test_the_spend_is_counted_for_today(state, verifier):
    verifier("verified")
    await add(state, "ana@acme.test")
    await add(state, "bo@acme.test")
    assert await spent_today(state) == 0
    report = await reverify_batch(state, env(hunter_api_key="k"), limit=2)
    assert report["checked"] == 2
    assert await spent_today(state) == 2


@pytest.mark.asyncio
async def test_the_best_prospects_are_checked_first(state, verifier):
    calls = verifier("verified")
    await add(state, "low@acme.test", score=10)
    await add(state, "high@acme.test", score=90)
    await reverify_batch(state, env(hunter_api_key="k"), limit=1)
    assert calls == ["high@acme.test"]


@pytest.mark.asyncio
async def test_the_batch_never_exceeds_the_days_budget(state, verifier):
    calls = verifier("verified")
    for i in range(5):
        await add(state, f"p{i}@acme.test")
    await record_spend(state, DAILY_BUDGET - 2)
    await reverify_batch(state, env(hunter_api_key="k"), limit=10)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_spent_account_stops_the_stage(state, verifier, monkeypatch):
    verifier("verified")
    await add(state, "ana@acme.test")

    async def no_credits(api_key):
        return {"daily": 0, "instant": 0, "active": True}

    monkeypatch.setattr(
        "sellbird.integrations.email_finder.reoon_balance", no_credits)
    report = await reverify_batch(state, env(reoon_api_key="k"))
    assert "skipped" in report
    assert report["checked"] == 0
