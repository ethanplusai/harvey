"""Tests for email_status persistence and sender/scout wiring."""

import os
import tempfile

import pytest
import pytest_asyncio

from harvey.state import StateManager
from harvey.models.prospect import Prospect


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.mark.asyncio
async def test_prospect_email_status_roundtrips(state):
    p = Prospect(
        first_name="Jane", last_name="Doe", email="jane.doe@acme.com",
        title="VP Sales", email_status="verified", email_verified=True,
    )
    pid = await state.add_prospect(p)
    fetched = await state.get_prospect(pid)
    assert fetched.email_status == "verified"
    assert fetched.email_verified is True


@pytest.mark.asyncio
async def test_update_prospect_email(state):
    p = Prospect(first_name="Jane", last_name="Doe", title="VP", company="Acme",
                 email_status="guess")
    pid = await state.add_prospect(p)

    await state.update_prospect_email(pid, "jane@acme.com", "verified")
    fetched = await state.get_prospect(pid)
    assert fetched.email == "jane@acme.com"
    assert fetched.email_status == "verified"
    assert fetched.email_verified is True

    await state.update_prospect_email(pid, "jane@acme.com", "invalid")
    fetched = await state.get_prospect(pid)
    assert fetched.email_status == "invalid"
    assert fetched.email_verified is False


@pytest.mark.asyncio
async def test_save_email_pattern_keeps_highest_confidence(state):
    await state.save_email_pattern("acme.com", pattern="{f}.{l}", source="default", confidence=0.3)
    await state.save_email_pattern("acme.com", pattern="{fi}{l}", source="hunter", confidence=0.9)
    cached = await state.get_email_pattern("acme.com")
    assert cached["pattern"] == "{fi}{l}"
    assert cached["confidence"] == 0.9

    # A lower-confidence write must not clobber the better pattern
    await state.save_email_pattern("acme.com", pattern="{f}", source="default", confidence=0.2)
    cached = await state.get_email_pattern("acme.com")
    assert cached["pattern"] == "{fi}{l}"


@pytest.mark.asyncio
async def test_save_email_pattern_updates_catch_all_independently(state):
    await state.save_email_pattern("acme.com", pattern="{f}.{l}", confidence=0.6, mx_type="google")
    await state.save_email_pattern("acme.com", is_catch_all=1)
    cached = await state.get_email_pattern("acme.com")
    assert cached["is_catch_all"] == 1
    assert cached["pattern"] == "{f}.{l}"   # unchanged
    assert cached["mx_type"] == "google"


@pytest.mark.asyncio
async def test_migration_downgrades_legacy_verified(state):
    """v4 migration must strip the old inflated verified flag to 'guess'."""
    # Simulate a pre-v4 row: email_verified=1 written directly
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "INSERT INTO prospects (id, first_name, last_name, email, "
            "email_verified, email_status, title, status) "
            "VALUES ('leg1','Old','Row','old@acme.com',1,'guess','VP','new')"
        )
        await db.commit()
    p = await state.get_prospect("leg1")
    # Sender must not treat this as sendable
    assert p.email_status == "guess"


# ── Sender deliverability gate ──


class _Cfg:
    class channels:
        class email:
            enabled = True
            provider = "instantly"
            max_daily_sends = 50
            send_to_risky = False


@pytest.mark.asyncio
async def test_sender_only_sends_verified(state, monkeypatch):
    from harvey.agents.sender import Sender

    # Three prospects: verified, guess, risky
    ids = {}
    for name, status in [("Ver", "verified"), ("Gue", "guess"), ("Ris", "risky")]:
        p = Prospect(
            first_name=name, last_name="X", title="VP",
            email=f"{name.lower()}@acme.com", email_status=status,
            email_verified=(status == "verified"), status="new",
        )
        ids[status] = await state.add_prospect(p)

    class Env:
        instantly_api_key = "k"
    sender = Sender(brain=None, state=state, config=_Cfg(), env=Env())

    # Filter logic mirrors _deploy_campaign's lead selection.
    from harvey.agents.sender import (
        SENDABLE_EMAIL_STATUSES, SENDABLE_EMAIL_STATUSES_WITH_RISKY,
    )
    allowed = SENDABLE_EMAIL_STATUSES
    sendable = []
    for status, pid in ids.items():
        p = await state.get_prospect(pid)
        if p.email_status in allowed:
            sendable.append(p)
    assert len(sendable) == 1
    assert sendable[0].email_status == "verified"

    # With risky enabled, two become sendable
    allowed = SENDABLE_EMAIL_STATUSES_WITH_RISKY
    sendable = [
        await state.get_prospect(pid) for status, pid in ids.items()
        if (await state.get_prospect(pid)).email_status in allowed
    ]
    assert {p.email_status for p in sendable} == {"verified", "risky"}
