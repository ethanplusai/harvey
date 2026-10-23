"""Send windows in the recipient's own time zone (#53): working out each
recipient's zone, the window maths (DST, weekends, windows that cross UTC
midnight), staging and draining through the outbox, Precleared's country
filling a gap, and quiet hours that no longer hold due mail.

No network and no real clock where it matters: the sender's clock is pinned
with monkeypatch, and Precleared calls go to an httpx.MockTransport.
"""

import asyncio
import json
import os
import random
import tempfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
import pytest_asyncio
import pytz
from pydantic import ValidationError

import harvey.agents.sender as sender_mod
from harvey.config import EmailChannelConfig, HarveyConfig, SendWindowConfig
from harvey.integrations.precleared import SETTINGS_PREFIX, PreclearedClient
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.send_window import START_SPREAD, in_window, next_send_time, zone_for
from harvey.state import MIGRATIONS, StateManager
from tests.test_outbox_native import FakeProvider, make_sender, seed_campaign

BASE = {
    "persona": {"name": "H", "company": "A", "role": "B", "email": "e", "linkedin": "", "tone": "t"},
    "product": {"name": "P", "description": "D", "pricing": "$1", "key_benefits": [],
                "objection_responses": {}},
    "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
}
WINDOW = SendWindowConfig()  # 08:00 to 17:00, weekdays, recipient's zone
WED_0200 = datetime(2026, 10, 21, 2, 0)  # a Wednesday, 02:00 UTC


def local(at: datetime, zone: str) -> datetime:
    return pytz.utc.localize(at).astimezone(pytz.timezone(zone))


def assert_office_hours(at: datetime, zone: str, window=WINDOW):
    when = local(at, zone)
    assert when.weekday() < 5, when
    assert window.start <= when.strftime("%H:%M") < window.end, when


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def window_sender(state, provider, monkeypatch, window=WINDOW, operator_zone="Europe/Oslo"):
    sender = make_sender(state, provider, require_approval=False)
    # The test config is a shared class: monkeypatch restores it afterwards.
    monkeypatch.setattr(sender.config.channels.email, "send_window", window, raising=False)
    monkeypatch.setattr(sender.config, "usage", SimpleNamespace(
        quiet_hours=SimpleNamespace(timezone=operator_zone)), raising=False)
    return sender


def pin_now(monkeypatch, at: datetime):
    monkeypatch.setattr(sender_mod, "_utcnow", lambda: at)


async def seed_at(state, location: str, email: str, status="queued") -> str:
    domain = email.split("@")[1]
    company_id = await state.add_company(Company(name=domain, domain=domain, location=location))
    return await state.add_prospect(Prospect(
        company_id=company_id, first_name="Jane", last_name="Doe", title="VP",
        company=domain, email=email, email_status="verified", email_verified=True,
        status=status))


async def approved(state, pid, to, kind="sequence", step=1, campaign="c1"):
    past = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)).isoformat()
    return await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="hello there",
        body="A short, plain note. Question?", send_at=past, status="approved",
        campaign_id=campaign if kind == "sequence" else "", step=step, kind=kind)


async def outbox_row(state, item_id):
    return await state.get_outbox_item(item_id)


# ── Working out the zone ──

@pytest.mark.parametrize("location, zone", [
    ("Austin, TX 78701", "America/Chicago"),
    ("Brooklyn, New York", "America/New_York"),
    ("Washington, DC", "America/New_York"),
    ("Oslo, Norway", "Europe/Oslo"),
    ("London EC1A 1BB, United Kingdom", "Europe/London"),
    ("Amsterdam, NL", "Europe/Amsterdam"),          # the Netherlands, not Newfoundland
    ("St. John's, NL, Canada", "America/St_Johns"),
    ("Toronto, ON", "America/Toronto"),
    ("Montréal, Québec", "America/Toronto"),
    ("Sydney NSW 2000", "Australia/Sydney"),
    ("Perth, WA, Australia", "Australia/Perth"),
    ("Berlin, Germany", "Europe/Berlin"),            # two zones, one clock
    ("Madrid, Spain", "Europe/Madrid"),              # mainland, not the Canaries
    ("Dublin, Ireland", "Europe/Dublin"),
])
def test_zone_from_location(location, zone):
    assert zone_for(location) == zone


@pytest.mark.parametrize("location", ["", "Remote", "United States", "Australia"])
def test_location_that_cannot_settle_a_zone(location):
    assert zone_for(location) == ""


def test_country_from_elsewhere():
    assert zone_for(country="GB") == "Europe/London"
    assert zone_for(country="uk") == "Europe/London"
    assert zone_for(country="US") == ""              # several zones
    assert zone_for(country="ZZ") == ""


def test_cached_coordinates_pick_between_a_countrys_zones():
    assert zone_for("Fortaleza, Brazil", point="-3.7300,-38.5200,40") == "America/Fortaleza"
    assert zone_for("Lyon", point="45.76,4.84") in pytz.country_timezones["FR"] + ["Europe/Monaco"]
    assert zone_for("Somewhere at sea", point="30.0,-40.0") == ""


def test_two_letter_code_that_is_a_state_and_a_country():
    # US convention first ("Wilmington, DE"), unless something says otherwise.
    assert zone_for("Berlin, DE") == "America/New_York"
    assert zone_for("Berlin, DE", country="DE") == "Europe/Berlin"
    assert zone_for("Berlin, DE", point="52.52,13.40") == "Europe/Berlin"
    assert zone_for("Perth, WA", point="-31.95,115.86") == "Australia/Perth"
    # A state written out wins over a country from elsewhere.
    assert zone_for("Austin, TX", country="GB") == "America/Chicago"


# ── The window ──

def test_new_york_and_oslo_staged_at_the_same_moment():
    ny = next_send_time(WED_0200, "America/New_York", WINDOW)
    oslo = next_send_time(WED_0200, "Europe/Oslo", WINDOW)
    assert_office_hours(ny, "America/New_York")
    assert_office_hours(oslo, "Europe/Oslo")
    assert local(ny, "America/New_York").date() == date(2026, 10, 21)
    assert local(oslo, "Europe/Oslo").date() == date(2026, 10, 21)
    assert oslo < ny  # Oslo's morning comes six hours sooner


def test_inside_the_window_sends_now():
    assert next_send_time(WED_0200, "Asia/Tokyo", WINDOW) == WED_0200  # 11:00 in Tokyo
    assert in_window(WED_0200, "Asia/Tokyo", WINDOW)
    assert not in_window(WED_0200, "America/New_York", WINDOW)


def test_weekends_wait_for_monday_unless_allowed():
    friday_evening = datetime(2026, 10, 16, 17, 0)  # 19:00 in Oslo
    monday = next_send_time(friday_evening, "Europe/Oslo", WINDOW)
    assert local(monday, "Europe/Oslo").strftime("%a") == "Mon"
    any_day = SendWindowConfig(weekdays_only=False)
    saturday = next_send_time(friday_evening, "Europe/Oslo", any_day)
    assert local(saturday, "Europe/Oslo").strftime("%a %H") == "Sat 08"


def test_opening_time_is_spread_but_stays_near_the_start():
    class Highest:
        @staticmethod
        def uniform(a, b):
            return b
    latest = next_send_time(WED_0200, "Europe/Oslo", WINDOW, rng=Highest)
    assert local(latest, "Europe/Oslo").strftime("%H:%M") == "08:30"
    assert START_SPREAD == timedelta(minutes=30)
    short = SendWindowConfig(start="08:00", end="09:00")
    assert local(next_send_time(WED_0200, "Europe/Oslo", short, rng=Highest),
                 "Europe/Oslo").strftime("%H:%M") == "08:15"  # a quarter of the window
    seeded = [next_send_time(WED_0200, "Europe/Oslo", WINDOW, rng=random.Random(n))
              for n in range(20)]
    assert len(set(seeded)) > 1


def test_dst_change_in_europe_keeps_local_office_hours():
    # Oslo moves from UTC+2 to UTC+1 on Sunday 25 October 2026.
    before = next_send_time(datetime(2026, 10, 16, 18, 0), "Europe/Oslo", WINDOW, rng=random.Random(1))
    after = next_send_time(datetime(2026, 10, 23, 18, 0), "Europe/Oslo", WINDOW, rng=random.Random(1))
    assert local(before, "Europe/Oslo").strftime("%a %H:%M") == local(after, "Europe/Oslo").strftime("%a %H:%M")
    assert after.hour - before.hour == 1  # same local time, an hour later in UTC


def test_dst_change_in_the_us_keeps_local_office_hours():
    # New York moves from UTC-4 to UTC-5 on Sunday 1 November 2026.
    monday = next_send_time(datetime(2026, 10, 30, 22, 0), "America/New_York", WINDOW)
    assert local(monday, "America/New_York").date() == date(2026, 11, 2)
    assert_office_hours(monday, "America/New_York")
    assert local(monday, "America/New_York").utcoffset() == timedelta(hours=-5)


def test_window_edges_on_the_clock_change_days():
    every_day = dict(weekdays_only=False)
    # 02:30 doesn't exist on 14 March 2027 in New York: the window opens
    # right after the gap.
    gap = SendWindowConfig(start="02:30", end="05:00", **every_day)
    opens = next_send_time(datetime(2027, 3, 14, 5, 0), "America/New_York", gap, rng=random.Random(0))
    assert local(opens, "America/New_York").strftime("%H:%M %Z")[:2] == "03"
    # 01:30 happens twice on 1 November 2026: the first one counts.
    twice = SendWindowConfig(start="01:30", end="03:00", **every_day)
    first = next_send_time(datetime(2026, 11, 1, 4, 0), "America/New_York", twice, rng=random.Random(0))
    assert local(first, "America/New_York").tzname() == "EDT"


def test_window_that_crosses_utc_midnight():
    # 08:00 to 17:00 in Los Angeles is 15:00 to 00:00 UTC.
    la = "America/Los_Angeles"
    just_before = datetime(2026, 10, 21, 23, 30)  # 16:30 there
    assert next_send_time(just_before, la, WINDOW) == just_before
    just_after = datetime(2026, 10, 22, 0, 30)    # 17:30 there
    nxt = next_send_time(just_after, la, WINDOW)
    assert local(nxt, la).date() == date(2026, 10, 22) and nxt.date() == date(2026, 10, 22)
    assert_office_hours(nxt, la)
    # In Sydney the working day starts the evening before in UTC, so the
    # weekday is judged locally: Sunday 21:30 UTC is Monday morning there.
    sydney = "Australia/Sydney"
    assert next_send_time(datetime(2026, 10, 25, 21, 30), sydney, WINDOW) == datetime(2026, 10, 25, 21, 30)
    saturday_there = datetime(2026, 10, 23, 22, 0)  # Saturday 09:00 in Sydney
    monday = next_send_time(saturday_there, sydney, WINDOW)
    assert local(monday, sydney).strftime("%a") == "Mon" and monday.strftime("%a") == "Sun"


# ── Config ──

def test_window_is_off_unless_configured():
    assert EmailChannelConfig().send_window is None
    assert HarveyConfig(**BASE).channels.email.send_window is None


def test_window_settings():
    w = SendWindowConfig()
    assert (w.start, w.end, w.weekdays_only, w.recipient_timezone) == ("08:00", "17:00", True, True)
    # YAML 1.1 turns an unquoted 17:00 into 1020.
    assert SendWindowConfig(start=480, end=1020).end == "17:00"
    with pytest.raises(ValidationError):
        SendWindowConfig(start="8am")
    with pytest.raises(ValidationError):
        SendWindowConfig(start="17:00", end="08:00")  # can't run past midnight
    cfg = HarveyConfig(**BASE, channels={"email": {
        "provider": "gmail", "send_window": {"start": "09:00", "end": "16:30"}}})
    assert cfg.channels.email.send_window.end == "16:30"


def test_which_configs_send_through_quiet_hours():
    from harvey.main import sends_through_quiet_hours

    def cfg(**email):
        return HarveyConfig(**BASE, channels={"email": {"provider": "gmail", **email}})
    assert sends_through_quiet_hours(cfg(send_window={}))
    assert not sends_through_quiet_hours(cfg())
    assert not sends_through_quiet_hours(cfg(send_window={"recipient_timezone": False}))
    assert not sends_through_quiet_hours(cfg(send_window={}, provider="instantly"))
    assert not sends_through_quiet_hours(cfg(send_window={}, enabled=False))


# ── Staging ──

@pytest.mark.asyncio
async def test_staged_emails_land_in_each_recipients_office_hours(state, monkeypatch):
    ny = await seed_at(state, "New York, NY", "jane@ny-acme.com", status="new")
    oslo = await seed_at(state, "Oslo, Norway", "kari@oslo-acme.no", status="new")
    campaign = await seed_campaign(state, [ny, oslo])  # step 2 waits 3 days
    pin_now(monkeypatch, WED_0200)
    await window_sender(state, FakeProvider(), monkeypatch)._stage_campaign_native(campaign)

    rows = await state.get_outbox(limit=10)
    zones = {ny: "America/New_York", oslo: "Europe/Oslo"}
    for row in rows:
        assert_office_hours(datetime.fromisoformat(row["send_at"]), zones[row["prospect_id"]])
    for pid, zone in zones.items():
        steps = {r["step"]: datetime.fromisoformat(r["send_at"]) for r in rows if r["prospect_id"] == pid}
        assert local(steps[1], zone).date() == date(2026, 10, 21)
        # Three days on is the weekend in both places: Monday morning.
        assert local(steps[2], zone).strftime("%a %Y-%m-%d") == "Mon 2026-10-26"
        assert (await state.get_prospect(pid)).timezone == zone  # stored once


@pytest.mark.asyncio
async def test_unknown_location_uses_the_quiet_hours_timezone(state, monkeypatch):
    pid = await seed_at(state, "", "sam@nowhere-acme.com", status="new")
    campaign = await seed_campaign(state, [pid])
    pin_now(monkeypatch, WED_0200)
    await window_sender(state, FakeProvider(), monkeypatch,
                        operator_zone="Asia/Tokyo")._stage_campaign_native(campaign)
    first = [r for r in await state.get_outbox(limit=10) if r["step"] == 1][0]
    assert datetime.fromisoformat(first["send_at"]) == WED_0200  # 11:00 in Tokyo: now
    assert (await state.get_prospect(pid)).timezone == ""        # nothing to remember


@pytest.mark.asyncio
async def test_one_zone_for_everyone_without_recipient_timezone(state, monkeypatch):
    pid = await seed_at(state, "New York, NY", "jane@ny-acme.com", status="new")
    campaign = await seed_campaign(state, [pid])
    pin_now(monkeypatch, WED_0200)
    one_zone = SendWindowConfig(recipient_timezone=False)
    await window_sender(state, FakeProvider(), monkeypatch, window=one_zone,
                        operator_zone="Europe/Oslo")._stage_campaign_native(campaign)
    first = [r for r in await state.get_outbox(limit=10) if r["step"] == 1][0]
    assert_office_hours(datetime.fromisoformat(first["send_at"]), "Europe/Oslo", one_zone)


@pytest.mark.asyncio
async def test_without_a_window_staging_is_unchanged(state, monkeypatch):
    pid = await seed_at(state, "New York, NY", "jane@ny-acme.com", status="new")
    campaign = await seed_campaign(state, [pid])
    pin_now(monkeypatch, WED_0200)
    await make_sender(state, FakeProvider())._stage_campaign_native(campaign)
    steps = {r["step"]: r["send_at"] for r in await state.get_outbox(limit=10)}
    assert steps == {1: WED_0200.isoformat(), 2: (WED_0200 + timedelta(days=3)).isoformat()}


# ── Draining ──

@pytest.mark.asyncio
async def test_due_cold_email_waits_for_its_window_and_replies_go(state, monkeypatch):
    ny = await seed_at(state, "New York, NY", "jane@ny-acme.com")
    tokyo = await seed_at(state, "Tokyo, Japan", "aiko@tokyo-acme.jp")
    replied = await seed_at(state, "New York, NY", "bob@ny-other.com", status="replied")
    waits = await approved(state, ny, "jane@ny-acme.com")
    await approved(state, tokyo, "aiko@tokyo-acme.jp")
    await approved(state, replied, "bob@ny-other.com", kind="reply")
    provider = FakeProvider()
    pin_now(monkeypatch, WED_0200)  # 22:00 on Tuesday in New York, 11:00 in Tokyo
    await window_sender(state, provider, monkeypatch)._drain_due()

    assert sorted(m["to"] for m in provider.sent) == ["aiko@tokyo-acme.jp", "bob@ny-other.com"]
    row = await outbox_row(state, waits)
    assert row["status"] == "approved"
    moved = datetime.fromisoformat(row["send_at"])
    assert_office_hours(moved, "America/New_York")
    assert local(moved, "America/New_York").date() == date(2026, 10, 21)


@pytest.mark.asyncio
async def test_follow_up_ready_time_moves_into_the_window(state, monkeypatch):
    pid = await seed_at(state, "New York, NY", "jane@ny-acme.com", status="contacted")
    campaign = await seed_campaign(state, [pid])  # step 2: 3 days after step 1
    first = await approved(state, pid, "jane@ny-acme.com", step=1, campaign=campaign.id)
    # Step 1 went out on Sunday 18 October at 18:30 New York time.
    await state.update_outbox_item(first, status="sent", sent_at="2026-10-18T22:30:00")
    second = await approved(state, pid, "jane@ny-acme.com", step=2, campaign=campaign.id)
    provider = FakeProvider()
    pin_now(monkeypatch, WED_0200)
    await window_sender(state, provider, monkeypatch)._drain_due()

    assert provider.sent == []
    ready = datetime.fromisoformat((await outbox_row(state, second))["send_at"])
    # Three days on is 18:30 on Wednesday, after hours: Thursday morning.
    assert local(ready, "America/New_York").strftime("%a %Y-%m-%d") == "Thu 2026-10-22"
    assert_office_hours(ready, "America/New_York")


OK_GB = {
    "verdict": "ok_to_email", "confidence": "high", "reasons": [],
    "facts": {"recipient_country": "GB", "country_source": "domain"},
    "receipt": {"id": "cc_rcpt_GB0000000001"},
}


def precleared_mock(calls, body):
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_precleared_country_sets_the_zone_when_nothing_else_does(state, monkeypatch):
    pid = await seed_at(state, "", "kate@uk-acme.co.uk")
    item = await approved(state, pid, "kate@uk-acme.co.uk")
    calls = []
    provider = FakeProvider()
    sender = window_sender(state, provider, monkeypatch, operator_zone="Asia/Tokyo")
    sender.precleared = PreclearedClient(sender_country="GB", transport=precleared_mock(calls, OK_GB))

    # 06:30 UTC: 15:30 in Tokyo (inside the fallback window), 07:30 in London.
    pin_now(monkeypatch, datetime(2026, 10, 21, 6, 30))
    await sender._drain_due()
    assert provider.sent == [] and len(calls) == 1
    assert (await state.get_prospect(pid)).timezone == "Europe/London"
    moved = datetime.fromisoformat((await outbox_row(state, item))["send_at"])
    assert_office_hours(moved, "Europe/London")

    # Later that morning in London it goes, on the cached verdict.
    pin_now(monkeypatch, datetime(2026, 10, 21, 9, 0))
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["kate@uk-acme.co.uk"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_precleared_keeps_the_country_with_the_cached_verdict(state):
    calls = []
    client = PreclearedClient(sender_country="GB", transport=precleared_mock(calls, OK_GB))
    first = await client.clear("kate@uk-acme.co.uk", state)
    again = await client.clear("kate@uk-acme.co.uk", state)
    assert first.country == again.country == "GB" and len(calls) == 1
    cached = json.loads(await state.get_setting(SETTINGS_PREFIX + "kate@uk-acme.co.uk"))
    assert cached["country"] == "GB"
    odd = PreclearedClient(sender_country="GB", transport=precleared_mock(
        [], {**OK_GB, "facts": {"recipient_country": "Great Britain"}}))
    assert (await odd.clear("x@y.co.uk", state)).country == ""


# ── Quiet hours ──

class _Brain:
    provider_label = "Claude"
    model = ""
    paused_reason = None

    def __init__(self, *a, **k):
        pass

    async def preflight(self):
        return None

    async def is_within_budget(self, *a, **k):
        return True


def _patch_heartbeat(monkeypatch, cfg, state, calls, stop):
    import harvey.agents.analyst
    import harvey.agents.handler
    import harvey.agents.scout
    import harvey.agents.writer
    import harvey.main as main_mod

    def agent(name):
        class Agent:
            is_native = True

            def __init__(self, *a, **k):
                pass

            async def run(self):
                calls.append(name)
                if name == "send":
                    stop.set()
                return True
        return Agent

    async def stop_instead_of_sleeping(seconds, event):
        event.set()
        return True

    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "load_env", lambda: SimpleNamespace())
    monkeypatch.setattr(main_mod, "StateManager", lambda: state)
    monkeypatch.setattr(main_mod, "Brain", _Brain)
    monkeypatch.setattr(main_mod, "in_quiet_hours", lambda c: True)
    monkeypatch.setattr(main_mod, "_interruptible_sleep", stop_instead_of_sleeping)
    monkeypatch.setattr(harvey.agents.scout, "Scout", agent("prospect"))
    monkeypatch.setattr(harvey.agents.writer, "Writer", agent("write"))
    monkeypatch.setattr(sender_mod, "Sender", agent("send"))
    monkeypatch.setattr(harvey.agents.handler, "Handler", agent("replies"))
    monkeypatch.setattr(harvey.agents.analyst, "Analyst", agent("analyze"))
    return main_mod


@pytest.mark.asyncio
async def test_quiet_hours_read_replies_then_send_what_is_due(state, monkeypatch):
    cfg = HarveyConfig(**BASE, channels={"email": {"provider": "gmail", "send_window": {}}})
    pid = await seed_at(state, "Tokyo, Japan", "aiko@tokyo-acme.jp")
    await approved(state, pid, "aiko@tokyo-acme.jp")
    calls, stop = [], asyncio.Event()
    main_mod = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == ["replies", "send"]  # no prospecting or writing


@pytest.mark.asyncio
async def test_quiet_hours_with_nothing_due_do_nothing(state, monkeypatch):
    cfg = HarveyConfig(**BASE, channels={"email": {"provider": "gmail", "send_window": {}}})
    calls, stop = [], asyncio.Event()
    main_mod = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == []


@pytest.mark.asyncio
async def test_without_recipient_zones_quiet_hours_hold_everything(state, monkeypatch):
    cfg = HarveyConfig(**BASE, channels={"email": {"provider": "gmail"}})
    pid = await seed_at(state, "Tokyo, Japan", "aiko@tokyo-acme.jp")
    await approved(state, pid, "aiko@tokyo-acme.jp")
    calls, stop = [], asyncio.Event()
    main_mod = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == []


# ── Storage ──

@pytest.mark.asyncio
async def test_existing_prospects_get_an_empty_zone_on_upgrade(tmp_path):
    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:11]:  # a database from before v12
            await db.executescript(script)
        await db.execute("PRAGMA user_version = 11")
        await db.execute("INSERT INTO prospects (id, email, first_name) VALUES ('p1', 'a@b.co', 'A')")
        await db.commit()
    sm = StateManager(db_path)
    await sm.init_db()
    assert (await sm.get_prospect("p1")).timezone == ""
    await sm.set_prospect_timezone("p1", "Europe/Oslo")
    assert (await sm.get_prospect("p1")).timezone == "Europe/Oslo"
