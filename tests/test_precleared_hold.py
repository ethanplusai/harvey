"""When the optional Precleared check holds cold email, the user is told.

The check holds cold email (never replies) when Precleared can't answer:
its free daily checks are used up, the key has no checks left, the key was
rejected, or it can't be reached. Until now that only reached the log, so a
user saw cold email go out slower and didn't know why. The sender now records
the hold under STATUS_KEY; the Today page and `sellbird status` read it and say
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

from sellbird.integrations.precleared import (
    STATUS_KEY,
    Clearance,
    current_hold,
    hold_message,
    record_hold,
)
from sellbird.state import StateManager
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
    jane = await seed_prospect(state, email="jane@example.com")
    sam = await seed_prospect(state, email="sam@example.net")
    await queue(state, jane, to="jane@example.com")
    await queue(state, sam, to="sam@example.net")
    ann = await seed_prospect(state, email="ann@example.org", status="replied")
    await queue(state, ann, kind="reply", to="ann@example.org")


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

    assert [m["to"] for m in provider.sent] == ["ann@example.org"]
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
    pid = await seed_prospect(state, email="jane@example.com")
    await queue(state, pid, to="jane@example.com")
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_status(200, OK_BODY))

    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == ["jane@example.com"]
    assert await state.get_setting(STATUS_KEY) == ""
    assert await current_hold(state) == {}


@pytest.mark.asyncio
async def test_a_cached_verdict_alone_does_not_clear_the_hold(state):
    pid = await seed_prospect(state, email="jane@example.com")
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_status(200, OK_BODY))
    await queue(state, pid, step=1, to="jane@example.com")
    await sender._drain_due()  # caches jane's verdict

    await record_hold(state, Clearance(allowed=False, held=True, hold_kind="keyless_limit"), 1)
    await days_pass(state)
    await queue(state, pid, step=2, to="jane@example.com")
    # Step 2 goes out on the cached verdict without asking Precleared, so
    # nothing says the allowance is back: the hold stays.
    await sender._drain_due()

    assert len(provider.sent) == 2
    assert (await current_hold(state))["kind"] == "keyless_limit"


@pytest.mark.asyncio
async def test_a_switched_off_check_records_nothing(state):
    # A hold recorded while the check was on, then the check is switched
    # off: cold email goes out and nothing says it waits on Precleared.
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(429, {"error": "daily_limit"}))
    await sender._drain_due()
    assert (await stored(state))["status"] == "held"

    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.precleared = None
    await sender._drain_due()

    assert sorted(m["to"] for m in provider.sent) == ["jane@example.com", "sam@example.net"]
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


def fake_config(enabled=True, provider="gmail"):
    from types import SimpleNamespace as NS
    return NS(compliance=NS(precleared=NS(enabled=enabled)),
              channels=NS(email=NS(provider=provider)))


def check_on(monkeypatch, **kwargs):
    import sellbird.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: fake_config(**kwargs))


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def test_today_shows_the_hold_with_its_link(client, monkeypatch):  # noqa: F811
    check_on(monkeypatch)
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
    check_on(monkeypatch)
    import sellbird.cli as cli
    import sellbird.state as state_mod

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


# ── integration fixes ──


@pytest.mark.asyncio
async def test_since_restarts_after_a_stale_gap(state):
    # A hold last seen days ago (Sellbird stopped, or the check was off) is
    # over: hitting the same kind again starts a new one, not "since" then.
    old = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0).isoformat()
    await state.set_setting(STATUS_KEY, json.dumps({
        "status": "held", "kind": "keyless_limit", "waiting": 1,
        "url": "", "reason": "", "since": old, "at": old,
    }))

    await record_hold(state, Clearance(allowed=False, held=True, hold_kind="keyless_limit"), 2)

    hold = await stored(state)
    assert hold["since"] == hold["at"]
    assert hold["since"] != old


@pytest.mark.asyncio
async def test_waiting_counts_every_cold_email_behind_the_hold(state):
    # More cold email is due than one cycle reads; the count must not stop
    # at the page size.
    for n in range(30):
        email = f"person{n}@example.com"
        pid = await seed_prospect(state, email=email)
        await queue(state, pid, to=email)
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(429, {"error": "daily_limit"}))

    await sender._drain_due()

    assert (await stored(state))["waiting"] == 30


@pytest.mark.asyncio
async def test_no_piece_of_the_key_reaches_the_hold(state):
    # An error body that echoes the key across the 200-character cut must
    # not leave its first characters in the reason shown on Today.
    key = "pc_live_Zq8vN3kW1xR7tB5yM2"
    body = "x" * 190 + key
    # 503: a busy server holds cold email whatever the addresses are.
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text=body))
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()), transport, api_key=key)

    await sender._drain_due()

    hold = await stored(state)
    assert hold["kind"] == "unavailable"
    assert key[:10] not in json.dumps(hold)
    assert key[:10] not in hold_message(hold)["detail"]


CONTROL = "\x1b]8;;https://evil.example\x07Click\x1b]8;;\x07\x1b[2J\x9b31m\x7f\x00"


def _has_control(text):
    import unicodedata
    return any(unicodedata.category(ch) == "Cc" for ch in str(text))


@pytest.mark.asyncio
async def test_control_characters_in_an_error_body_never_reach_the_hold(state):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, text="down " + CONTROL + " try later"))
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()), transport)

    await sender._drain_due()

    hold = await stored(state)
    assert hold["kind"] == "unavailable"
    assert "try later" in hold["reason"]
    assert not _has_control(hold["reason"])


def test_hold_message_strips_control_characters_from_a_stored_hold():
    for kind in ("keyless_limit", "out_of_checks", "key_rejected", "unavailable"):
        message = hold_message({"kind": kind, "waiting": 1,
                                "reason": "down " + CONTROL,
                                "url": "https://precleared.dev/buy" + CONTROL})
        for value in message.values():
            assert not _has_control(value), (kind, value)


def test_cli_status_never_prints_control_characters(monkeypatch, tmp_path, capsys):
    import sellbird.cli as cli
    import sellbird.state as state_mod

    check_on(monkeypatch)
    db = tmp_path / "status.db"
    _seed(db, json.dumps({"status": "held", "kind": "unavailable", "waiting": 1,
                          "url": "", "since": _now() + CONTROL, "at": _now(),
                          "reason": "down " + CONTROL}))
    monkeypatch.setattr(state_mod, "StateManager", lambda: StateManager(str(db)))

    cli.cmd_status(type("A", (), {"check": False})())

    out = capsys.readouterr().out
    assert "Precleared:" in out
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["json", "percent", "percent-lower", "percent-twice"])
async def test_an_escaped_or_encoded_key_is_redacted_too(state, form):
    import re
    from urllib.parse import quote
    from sellbird.integrations.precleared import PreclearedClient

    key = "pc_live_Ab/Cd+Ef=Gh9"
    echoed = {
        "json": key.replace("/", "\\/"),
        "percent": quote(key, safe=""),
        "percent-lower": re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(),
                                quote(key, safe="")),
        "percent-twice": quote(quote(key, safe=""), safe=""),
    }[form]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, text=f"bad auth for {echoed} today"))
    client = PreclearedClient("GB", api_key=key, transport=transport)

    result = await client.clear("person@example.com", state)

    assert result.held and result.hold_kind == "unavailable"
    assert echoed not in result.reason
    assert "Ab" + "/" not in result.reason and "Cd" not in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://evil.example/pay",
    "https://precleared.dev.evil.example/buy",
    "https://evilprecleared.dev/buy",
])
async def test_a_checkout_link_off_precleared_falls_back_to_the_buy_page(state, url):
    from sellbird.integrations.precleared import BUY_URL, PreclearedClient

    client = PreclearedClient("GB", api_key="pc_live_test",
                              transport=mock_status(402, {"buy_url": url}))

    result = await client.clear("person@example.com", state)

    assert result.hold_kind == "out_of_checks"
    assert result.fix_url == BUY_URL
    assert url not in result.reason


@pytest.mark.asyncio
async def test_a_checkout_link_on_precleared_is_kept(state):
    from sellbird.integrations.precleared import PreclearedClient

    url = "https://pay.precleared.dev/session/example123"
    client = PreclearedClient("GB", api_key="pc_live_test",
                              transport=mock_status(402, {"buy_url": url}))

    result = await client.clear("person@example.com", state)

    assert result.fix_url == url


@pytest.mark.asyncio
async def test_count_due_cold_outbox_counts_only_due_approved_cold_email(state):
    from sellbird.agents.sender import Sender  # noqa: F401 (schema in place)

    a = await seed_prospect(state, email="a@example.com")
    b = await seed_prospect(state, email="b@example.com")
    c = await seed_prospect(state, email="c@example.com", status="replied")
    await queue(state, a, to="a@example.com")                 # counts
    await queue(state, a, step=2, to="a@example.com")         # step 1 still queued
    await queue(state, b, to="b@example.com")                 # counts
    await queue(state, c, kind="reply", to="c@example.com")   # a reply
    later = await queue(state, b, to="b@example.com", campaign="c2")
    await state.update_outbox_item(later, send_at="2999-01-01T00:00:00")
    pending = await queue(state, b, to="b@example.com", campaign="c3")
    await state.update_outbox_item(pending, status="pending_review")

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert await state.count_due_cold_outbox(now) == 2


@pytest.mark.asyncio
async def test_a_held_cycle_does_not_read_a_huge_page(state):
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(429, {"error": "daily_limit"}))
    limits = []
    real = state.get_outbox

    async def spy(*args, **kwargs):
        limits.append(kwargs.get("limit", 200))
        return await real(*args, **kwargs)

    state.get_outbox = spy
    await sender._drain_due()

    assert (await stored(state))["waiting"] == 2
    assert max(limits) <= 24


@pytest.mark.asyncio
async def test_the_hold_is_hidden_when_the_check_does_not_apply(state):
    await record_hold(state, Clearance(allowed=False, held=True, hold_kind="key_rejected"), 2)

    assert (await current_hold(state, fake_config()))["kind"] == "key_rejected"
    assert await current_hold(state, fake_config(enabled=False)) == {}
    assert await current_hold(state, fake_config(provider="instantly")) == {}
    # Without a config to judge by (it failed to load), the hold still shows.
    assert (await current_hold(state))["kind"] == "key_rejected"


def test_today_hides_the_hold_when_the_check_is_off(client, monkeypatch):  # noqa: F811
    check_on(monkeypatch, enabled=False)
    _seed(client.db_path, json.dumps({
        "status": "held", "kind": "key_rejected", "url": "", "waiting": 3,
        "since": _now(), "at": _now(), "reason": "",
    }))

    items = client.get("/api/today").json()["items"]

    assert not [i for i in items if i["key"] == "precleared-hold"]


def test_cli_status_hides_the_hold_when_the_provider_is_not_native(monkeypatch, tmp_path, capsys):
    import sellbird.cli as cli
    import sellbird.state as state_mod

    check_on(monkeypatch, provider="instantly")
    db = tmp_path / "status.db"
    _seed(db, json.dumps({"status": "held", "kind": "keyless_limit", "waiting": 2,
                          "url": "", "since": _now(), "at": _now(), "reason": ""}))
    monkeypatch.setattr(state_mod, "StateManager", lambda: StateManager(str(db)))

    cli.cmd_status(type("A", (), {"check": False})())

    assert "Precleared:" not in capsys.readouterr().out
