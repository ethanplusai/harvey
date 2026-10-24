"""When the optional Precleared check holds cold email, the user is told.

The check holds cold email (never replies) when Precleared can't answer:
its free daily checks are used up, the key has no checks left, the key was
rejected, or it can't be reached. Until now that only reached the log, so a
user saw cold email go out slower and didn't know why. The sender now records
the hold under STATUS_KEY; the Today page and `harvey status` read it and say
how to fix it. No network: every Precleared call goes to an httpx.MockTransport.
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from harvey.integrations.precleared import (
    STATUS_KEY,
    Clearance,
    current_hold,
    hold_message,
    record_hold,
)
from harvey.state import StateManager
from tests.test_dashboard_signals import client  # noqa: F401 (fixture)
from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect
from tests.test_precleared import OK_BODY, days_pass, mock_status, queue, with_check

CHECKOUT = "https://precleared.dev/pay/AbCdEfGhIjKlMnOpQrStUvWx?p=standard&src=402"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


async def two_cold_and_a_reply(state):
    jane = await seed_prospect(state)
    sam = await seed_prospect(state, email="sam@beta.com")
    await queue(state, jane)
    await queue(state, sam, to="sam@beta.com")
    ann = await seed_prospect(state, email="ann@gamma.com", status="replied")
    await queue(state, ann, kind="reply", to="ann@gamma.com")


async def stored(state):
    return json.loads(await state.get_setting(STATUS_KEY) or "{}")


# ── what the sender records ──


@pytest.mark.asyncio
async def test_keyless_limit_records_a_hold_pointing_to_a_free_key(state):
    await two_cold_and_a_reply(state)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider),
                        mock_status(429, {"error": "daily_limit"}))

    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == ["ann@gamma.com"]
    hold = await stored(state)
    assert hold["status"] == "held"
    assert hold["kind"] == "keyless_limit"
    assert hold["url"] == "https://precleared.dev/get-key"
    assert hold["waiting"] == 2
    assert hold["since"] == hold["at"]


@pytest.mark.asyncio
async def test_key_out_of_checks_records_its_checkout_link(state):
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(402, {"error": "payment_required", "buy_url": CHECKOUT}),
                        api_key="pc_live_test")

    await sender._drain_due()

    hold = await stored(state)
    assert hold["kind"] == "out_of_checks"
    assert hold["url"] == CHECKOUT
    assert "pc_live_test" not in json.dumps(hold)


@pytest.mark.asyncio
async def test_rejected_key_and_outage_are_recorded_too(state):
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(401, {"error": "invalid_key"}), api_key="pc_live_test")
    await sender._drain_due()
    assert (await stored(state))["kind"] == "key_rejected"

    def boom(request):
        raise httpx.ConnectError("no route to host")

    sender.precleared._transport = httpx.MockTransport(boom)
    await sender._drain_due()
    hold = await stored(state)
    assert hold["kind"] == "unavailable"
    assert hold["url"] == ""
    assert "no route to host" in hold["reason"]


@pytest.mark.asyncio
async def test_since_is_kept_while_the_same_hold_continues(state):
    first = Clearance(allowed=False, held=True, hold_kind="out_of_checks", fix_url=CHECKOUT)
    await record_hold(state, first, 3)
    hold = await stored(state)
    hold["since"] = "2026-10-21T08:00:00+00:00"
    await state.set_setting(STATUS_KEY, json.dumps(hold))

    await record_hold(state, first, 5)
    again = await stored(state)
    assert again["since"] == "2026-10-21T08:00:00+00:00"
    assert again["waiting"] == 5

    await record_hold(state, Clearance(allowed=False, held=True, hold_kind="key_rejected"), 1)
    assert (await stored(state))["since"] != "2026-10-21T08:00:00+00:00"


@pytest.mark.asyncio
async def test_a_fresh_answer_clears_the_hold(state):
    await record_hold(state, Clearance(allowed=False, held=True, hold_kind="keyless_limit",
                                       fix_url="https://precleared.dev/get-key"), 2)
    pid = await seed_prospect(state)
    await queue(state, pid)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_status(200, OK_BODY))

    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == ["jane@acme.com"]
    assert await state.get_setting(STATUS_KEY) == ""
    assert await current_hold(state) == {}


@pytest.mark.asyncio
async def test_a_cached_verdict_alone_does_not_clear_the_hold(state):
    pid = await seed_prospect(state)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_status(200, OK_BODY))
    await queue(state, pid, step=1)
    await sender._drain_due()  # caches jane's verdict

    await record_hold(state, Clearance(allowed=False, held=True, hold_kind="keyless_limit"), 1)
    await days_pass(state)
    await queue(state, pid, step=2)
    # Step 2 goes out on the cached verdict without asking Precleared, so
    # nothing says the allowance is back: the hold stays.
    await sender._drain_due()

    assert len(provider.sent) == 2
    assert (await current_hold(state))["kind"] == "keyless_limit"


@pytest.mark.asyncio
async def test_a_switched_off_check_records_nothing(state):
    await two_cold_and_a_reply(state)
    sender = make_sender(state, FakeProvider())
    sender.precleared = None

    await sender._drain_due()

    assert await state.get_setting(STATUS_KEY) == ""


# ── reading it back ──


@pytest.mark.asyncio
async def test_a_stale_or_malformed_hold_is_ignored(state):
    old = (datetime.now(timezone.utc) - timedelta(hours=27)).isoformat()
    await state.set_setting(STATUS_KEY, json.dumps(
        {"status": "held", "kind": "keyless_limit", "at": old, "since": old}))
    assert await current_hold(state) == {}

    for raw in ["", "not json", "[]", json.dumps({"status": "held"}),
                json.dumps({"status": "held", "at": "yesterday"})]:
        await state.set_setting(STATUS_KEY, raw)
        assert await current_hold(state) == {}


def test_messages_say_how_to_fix_each_hold():
    keyless = hold_message({"kind": "keyless_limit", "waiting": 1,
                            "url": "https://precleared.dev/get-key"})
    assert keyless["title"] == "Precleared's free checks for today are used up"
    assert keyless["detail"].startswith("1 cold email is waiting.")
    assert "PRECLEARED_API_KEY" in keyless["detail"]
    assert keyless["url_label"] == "Get a free key"

    out = hold_message({"kind": "out_of_checks", "waiting": 12, "url": CHECKOUT})
    assert out["detail"].startswith("12 cold emails are waiting.")
    assert f"Buy checks to send them now: {CHECKOUT}." in out["detail"]
    assert out["url"] == CHECKOUT

    rejected = hold_message({"kind": "key_rejected", "waiting": 2})
    assert rejected["tone"] == "bad"
    assert rejected["url"] == "https://precleared.dev/get-key"

    down = hold_message({"kind": "unavailable", "waiting": "x", "reason": "Precleared HTTP 503"})
    assert down["url"] == ""
    assert "1 cold email is waiting until it answers (Precleared HTTP 503)" in down["detail"]

    for message in (keyless, out, rejected, down):
        assert "Replies are not held." in message["detail"]


# ── where the user sees it ──


def _seed(db_path, value):
    async def go():
        sm = StateManager(str(db_path))
        await sm.init_db()
        await sm.set_setting(STATUS_KEY, value)
    asyncio.run(go())


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def test_today_shows_the_hold_with_its_link(client):  # noqa: F811
    _seed(client.db_path, json.dumps({
        "status": "held", "kind": "out_of_checks", "url": CHECKOUT, "waiting": 3,
        "since": _now(), "at": _now(), "reason": "",
    }))

    items = client.get("/api/today").json()["items"]

    item = next(i for i in items if i["key"] == "precleared-hold")
    assert item["title"] == "Your Precleared key is out of checks for today"
    assert item["detail"].startswith("3 cold emails are waiting.")
    assert item["url"] == CHECKOUT
    assert item["tab"] == "controls"


def test_today_says_nothing_without_a_hold(client):  # noqa: F811
    items = client.get("/api/today").json()["items"]
    assert not [i for i in items if i["key"] == "precleared-hold"]


def test_cli_status_shows_the_hold(monkeypatch, tmp_path, capsys):
    import harvey.cli as cli
    import harvey.state as state_mod

    db = tmp_path / "status.db"
    since = _now()
    _seed(db, json.dumps({"status": "held", "kind": "keyless_limit", "waiting": 2,
                          "url": "https://precleared.dev/get-key", "since": since,
                          "at": since, "reason": ""}))
    monkeypatch.setattr(state_mod, "StateManager", lambda: StateManager(str(db)))

    cli.cmd_status(type("A", (), {"check": False})())

    out = capsys.readouterr().out
    assert f"Precleared:           HOLDING COLD EMAIL since {since}" in out
    assert "2 cold emails are waiting" in out
    assert "PRECLEARED_API_KEY" in out
