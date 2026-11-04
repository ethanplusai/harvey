"""Bounces: only Harvey's own count, the reason and how the address was found
are kept, and the Outbox shows them with what to tighten."""

import json

import pytest
from fastapi.testclient import TestClient

from harvey import bounces as b
from harvey.integrations.mail_provider import InboundMessage
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, _now_iso, make_handler, seed_prospect, state,
)

NDR = """Delivery has failed to these recipients or groups:

jane@acme.com
The email address you entered couldn't be found.

Diagnostic information for administrators:
Remote server returned '550 5.1.1 RESOLVER.ADR.RecipNotFound; not found'
"""


# ── reading a bounce ──

def test_reason_kind_and_addresses():
    assert b.bounce_reason(NDR).startswith("550 5.1.1 RESOLVER.ADR.RecipNotFound")
    assert b.bounce_kind(b.bounce_reason(NDR)) == "no_mailbox"
    assert b.bounce_kind("552 5.2.2 Mailbox full") == "mailbox_full"
    assert b.bounce_kind("550 5.7.1 Message rejected as spam") == "blocked"
    assert b.bounce_kind("") == "other"
    assert b.failed_addresses("Undeliverable: hello", NDR,
                              {"X-Failed-Recipients": "Jane@Acme.com"}) == ["jane@acme.com"]


def test_suggestions_follow_the_cause():
    finder = b.suggestions("no_mailbox", source="finder:apollo", email_status="verified", mx_type="microsoft")
    assert any("ZEROBOUNCE_API_KEY" in t and "finder" in t for t in finder)
    assert any("Microsoft 365" in t for t in finder)
    built = b.suggestions("no_mailbox", source="company_website", email_status="verified")
    assert any("EMAIL_FINDERS" in t for t in built)
    catch_all = b.suggestions("no_mailbox", email_status="risky", send_to_risky=True)
    assert any("Turn channels.email.send_to_risky off" in t for t in catch_all)
    blocked = b.suggestions("blocked", source="finder:apollo")
    assert len(blocked) == 1 and "harvey warmup" in blocked[0]
    assert b.how_found("finder:prospeo") == "email finder (Prospeo)"
    assert b.how_found("company_inbox") == "general inbox published on their site"


# ── the handler ──

async def sent_to(state, pid, email="jane@acme.com", n=1, message_id="<m1@x>"):
    for i in range(n):
        oid = await state.add_outbox_item(
            prospect_id=pid, to_email=email, subject="s", body="b", send_at=_now_iso(),
            status="approved", campaign_id=f"c{i}", step=1)
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso(),
                                       message_id=message_id if i == 0 else f"<m{i}@x>")


@pytest.mark.asyncio
async def test_a_bounce_of_the_owners_own_mail_is_not_counted(state):
    pid = await seed_prospect(state, status="contacted")
    await sent_to(state, pid, n=11)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="own", from_email="postmaster@outlook.com",
        subject="Undeliverable: Re: Quarterly invoice", body=NDR.replace("jane@acme.com", "buyer@supplier.example"),
        in_reply_to="<owner-thread@mail>", is_bounce=True)]
    await make_handler(state, provider)._run_native()

    assert await state.get_setting("sending_paused") in ("", None)
    assert int(await state.get_setting("bounce_count") or 0) == 0
    assert (await state.get_prospect(pid)).email_status == "verified"


@pytest.mark.asyncio
async def test_harvey_bounce_matched_by_address_when_in_reply_to_is_missing(state):
    pid = await seed_prospect(state, status="contacted")
    await sent_to(state, pid, n=11)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="ndr", from_email="postmaster@acme.com",
        subject="Undeliverable: hi Jane", body=NDR, in_reply_to="", is_bounce=True)]
    await make_handler(state, provider)._run_native()

    assert (await state.get_prospect(pid)).email_status == "invalid"
    paused = await state.get_setting("sending_paused")
    assert "latest: jane@acme.com" in paused and "doesn't exist" in paused
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        (raw,) = await (await db.execute(
            "SELECT details_json FROM actions WHERE action_type = 'bounce'")).fetchone()
    d = json.loads(raw)
    assert d["prospect"] == "jane@acme.com" and d["matched_by"] == "address"
    assert d["kind"] == "no_mailbox" and d["email_status"] == "verified"
    assert d["reason"].startswith("550 5.1.1") and d["suggestions"]


# ── the Outbox ──

@pytest.fixture
def client(tmp_path, monkeypatch):
    import harvey.dashboard as dash
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        c.db = tmp_path / "data" / "harvey.db"
        yield c


def test_outbox_lists_bounces_and_ignored_count(client):
    import asyncio

    from harvey.state import StateManager

    async def seed():
        s = StateManager(str(client.db))
        await s.init_db()
        await s.log_action("bounce", "handler", {
            "prospect": "jane@acme.com", "company": "Acme", "kind": "no_mailbox",
            "reason": "550 5.1.1 not found", "found_by": "email finder (Apollo)",
            "suggestions": ["Add a second verifier"]})
        await s.log_action("bounce_ignored", "handler", {"subject": "Undeliverable: other mail"})
    asyncio.run(seed())
    data = client.get("/api/bounces").json()
    assert [x["prospect"] for x in data["bounces"]] == ["jane@acme.com"]
    assert data["bounces"][0]["suggestions"] == ["Add a second verifier"]
    assert data["bounces_ignored"] == 1


# ── The mailbox monitor: other mail in the mailbox bouncing ──

from tests.test_outbox_native import Cfg  # noqa: E402


def other_mail_bounce(i):
    return InboundMessage(
        provider_id=f"other{i}", from_email="postmaster@outlook.com",
        subject=f"Undeliverable: Re: Order {i}",
        body=NDR.replace("jane@acme.com", f"buyer{i}@supplier.example"),
        in_reply_to=f"<not-harvey-{i}@mail>", is_bounce=True)


@pytest.mark.asyncio
async def test_mailbox_monitor_pauses_after_the_limit(state, monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "mailbox_bounce_limit", 3, raising=False)
    monkeypatch.setattr(Cfg.channels.email, "mailbox_bounce_days", 7, raising=False)
    provider = FakeProvider()
    handler = make_handler(state, provider)

    provider.inbound = [other_mail_bounce(1), other_mail_bounce(2)]
    await handler._run_native()
    assert await state.get_setting("sending_paused") in ("", None)

    provider.inbound = [other_mail_bounce(3)]
    await handler._run_native()
    paused = await state.get_setting("sending_paused")
    assert paused.startswith("3 bounces of mail Harvey didn't send in the last 7 days (limit 3)")
    assert "another mailbox" in paused
    # Harvey's own bounce count is untouched: two separate monitors.
    assert int(await state.get_setting("bounce_count") or 0) == 0


@pytest.mark.asyncio
async def test_resume_starts_the_mailbox_count_afresh(state, monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "mailbox_bounce_limit", 2, raising=False)
    provider = FakeProvider()
    handler = make_handler(state, provider)
    provider.inbound = [other_mail_bounce(1), other_mail_bounce(2)]
    await handler._run_native()
    assert await state.get_setting("sending_paused")

    await state.resume_sending()
    assert await state.get_setting("sending_paused") == ""
    provider.inbound = [other_mail_bounce(3)]
    await handler._run_native()
    assert await state.get_setting("sending_paused") == ""   # 1 since the resume


@pytest.mark.asyncio
async def test_mailbox_monitor_off_at_zero(state, monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "mailbox_bounce_limit", 0, raising=False)
    provider = FakeProvider()
    provider.inbound = [other_mail_bounce(i) for i in range(5)]
    await make_handler(state, provider)._run_native()
    assert await state.get_setting("sending_paused") in ("", None)


def test_window_and_resume_position():
    from datetime import datetime
    assert b.mailbox_window_start(7, datetime(2026, 1, 10, 12, 0, 0)) == "2026-01-03 12:00:00"
    assert b.resumed_after_row("42") == 42
    assert b.resumed_after_row("") == 0 and b.resumed_after_row("x") == 0


def test_config_validates_the_mailbox_monitor():
    from pydantic import ValidationError

    from harvey.config import EmailChannelConfig
    assert EmailChannelConfig().mailbox_bounce_limit == 10
    with pytest.raises(ValidationError):
        EmailChannelConfig(mailbox_bounce_limit=-1)
    with pytest.raises(ValidationError):
        EmailChannelConfig(mailbox_bounce_days=0)


def test_outbox_shows_both_monitors(client):
    import asyncio

    from harvey.state import StateManager

    async def seed():
        s = StateManager(str(client.db))
        await s.init_db()
        for _ in range(2):
            await s.log_action("bounce_ignored", "handler", {"subject": "Undeliverable: other"})
        await s.set_setting("bounce_count", "1")
    asyncio.run(seed())
    stats = client.get("/api/bounces").json()["bounce_stats"]
    assert {k: stats["mailbox"][k] for k in ("count", "limit", "days")} == {"count": 2, "limit": 10, "days": 7}
    assert stats["mailbox"]["setting"] == "channels.email.mailbox_bounce_limit" and stats["mailbox"]["default"] == 10
    assert stats["harvey"]["setting"] == "channels.email.max_bounce_rate" and stats["harvey"]["min_sends"] == 10
    assert stats["harvey"]["bounced"] == 1 and stats["harvey"]["limit_rate"] == 0.05
    client.post("/api/sending/resume")
    assert client.get("/api/bounces").json()["bounce_stats"]["mailbox"]["count"] == 0
    assert "bounces" not in client.get("/api/outbox").json()   # bounces live on Controls


def test_older_unmatched_bounce_records_count_as_other_mail(client):
    """Before this change, a bounce Harvey couldn't match was logged as
    'bounce' with prospect "unknown". Those are other mail: not listed as
    Harvey's, and counted by the mailbox monitor."""
    import asyncio

    from harvey.state import StateManager

    async def seed():
        s = StateManager(str(client.db))
        await s.init_db()
        for _ in range(3):
            await s.log_action("bounce", "handler", {"prospect": "unknown", "bounces": 1, "total_sent": 13})
    asyncio.run(seed())
    data = client.get("/api/bounces").json()
    assert data["bounces"] == [] and data["bounces_ignored"] == 3
    assert data["bounce_stats"]["mailbox"]["count"] == 3
