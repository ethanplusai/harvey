"""Send now: one approved email goes out past the daily cap and the send
window, and nothing else is skipped."""

from datetime import date, datetime, timedelta, timezone

import pytest

from sellbird.config import SendWindowConfig, WarmupConfig
from sellbird.state import SEND_NOW_PREFIX, SEND_NOW_TTL
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_prospect, state,
)


async def _approved(state, address, **fields):
    pid = await seed_prospect(state, email=address, status="queued", **fields)
    return await state.add_outbox_item(
        prospect_id=pid, to_email=address, subject="hello there",
        body="A short, plain note. Question?",
        send_at=(datetime.now(timezone.utc).replace(tzinfo=None)
                 - timedelta(minutes=1)).isoformat(),
        status="approved", campaign_id="c1", step=1)


def _warming(sender, monkeypatch):
    """A mailbox still in warmup: cold email has a cap of zero today."""
    start = (date.today() - timedelta(days=3)).isoformat()
    monkeypatch.setattr(sender.config.channels.email, "warmup",
                        WarmupConfig(start_date=start, min_days=14), raising=False)


@pytest.mark.asyncio
async def test_a_requested_email_goes_past_the_daily_cap(state, monkeypatch):
    wanted = await _approved(state, "wanted@acme.com")
    waiting = await _approved(state, "waiting@acme.com")
    assert await state.request_send_now(wanted)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    _warming(sender, monkeypatch)
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["wanted@acme.com"]
    assert (await state.get_outbox_item(waiting))["status"] == "approved"
    assert await state.send_now_ids() == set()  # acted on once


@pytest.mark.asyncio
async def test_a_requested_email_goes_outside_the_send_window(state, monkeypatch):
    item_id = await _approved(state, "wanted@acme.com")
    await state.request_send_now(item_id)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    # A window that is closed right now, whatever the time.
    closed = SendWindowConfig(start="00:00", end="00:01", weekdays_only=True)
    monkeypatch.setattr(sender.config.channels.email, "send_window", closed, raising=False)
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["wanted@acme.com"]


@pytest.mark.asyncio
async def test_a_requested_email_still_meets_the_other_checks(state, monkeypatch):
    item_id = await _approved(state, "wanted@acme.com", estatus="risky")
    await state.request_send_now(item_id)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    assert (await state.get_outbox_item(item_id))["status"] != "sent"


@pytest.mark.asyncio
async def test_a_lapsed_request_is_dropped(state):
    item_id = await _approved(state, "wanted@acme.com")
    old = (datetime.now(timezone.utc).replace(tzinfo=None)
           - SEND_NOW_TTL - timedelta(minutes=1)).isoformat()
    await state.set_setting(SEND_NOW_PREFIX + item_id, old)
    assert await state.send_now_ids() == set()


@pytest.mark.asyncio
async def test_without_a_request_the_cap_and_window_hold_the_email(state, monkeypatch):
    item_id = await _approved(state, "plain@acme.com")
    provider = FakeProvider()
    sender = make_sender(state, provider)
    _warming(sender, monkeypatch)
    await sender._drain_due()
    assert provider.sent == []
    assert (await state.get_outbox_item(item_id))["status"] == "approved"


@pytest.mark.asyncio
async def test_a_new_request_wakes_the_sleeping_loop(state, monkeypatch):
    import asyncio
    from sellbird import main
    monkeypatch.setattr(main, "SEND_NOW_POLL_SECONDS", 0.05)
    item_id = await _approved(state, "wanted@acme.com")
    stop = asyncio.Event()
    sleeping = asyncio.create_task(main._sleep_until_next_cycle(60, stop, state))
    await asyncio.sleep(0.1)
    await state.request_send_now(item_id)
    assert await asyncio.wait_for(sleeping, timeout=2) is False


@pytest.mark.asyncio
async def test_a_request_the_cycle_already_saw_does_not_wake_it(state, monkeypatch):
    import asyncio
    from sellbird import main
    monkeypatch.setattr(main, "SEND_NOW_POLL_SECONDS", 0.05)
    item_id = await _approved(state, "wanted@acme.com")
    await state.request_send_now(item_id)
    stop = asyncio.Event()
    started = asyncio.get_running_loop().time()
    await main._sleep_until_next_cycle(0.3, stop, state)
    assert asyncio.get_running_loop().time() - started >= 0.3
