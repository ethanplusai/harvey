"""Send windows in the recipient's own time zone (#53): working out each
recipient's zone, the window maths (DST, weekends, windows that cross UTC
midnight), staging and draining through the outbox, Precleared's country
filling a gap, and quiet hours that no longer hold due mail. Also the
country Sellbird works out the same way and gives Precleared, when it is sure.

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

import sellbird.agents.sender as sender_mod
from sellbird.config import EmailChannelConfig, SellbirdConfig, SendWindowConfig
from sellbird.integrations.precleared import SETTINGS_PREFIX, PreclearedClient
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.send_window import (
    START_SPREAD, country_for, icp_country, in_window, next_send_time, recipient_country,
    recipient_zone, zone_country, zone_for,
)
from sellbird.state import MIGRATIONS, StateManager
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


@pytest.mark.parametrize("location", ["", "Remote", "United States", "Houston"])
def test_location_that_cannot_settle_a_zone(location):
    assert zone_for(location) == ""


@pytest.mark.parametrize("location, country, zone", [
    # Countries with several zones and nothing more precise: the main one.
    ("Melbourne, Australia", "", "Australia/Sydney"),
    ("Jakarta, Indonesia", "", "Asia/Jakarta"),
    ("Moscow, Russia", "", "Europe/Moscow"),
    ("Toronto, Canada", "", "America/Toronto"),
    ("Recife, Brazil", "", "America/Sao_Paulo"),
    ("Monterrey, Mexico", "", "America/Mexico_City"),
    ("", "AU", "Australia/Sydney"),
    ("", "ID", "Asia/Jakarta"),
    ("", "RU", "Europe/Moscow"),
    # The US is left to the operator's zone: no single main one.
    ("", "US", ""),
])
def test_main_zone_of_a_country_with_several(location, country, zone):
    assert zone_for(location, country=country) == zone


def test_more_precise_than_the_main_zone_still_wins():
    assert zone_for("Perth, WA, Australia") == "Australia/Perth"
    assert zone_for("Vancouver, BC") == "America/Vancouver"
    assert zone_for("Novosibirsk, Russia", point="55.03,82.92") == "Asia/Novosibirsk"


def test_washington_dc_with_dots():
    assert zone_for("Washington, D.C.") == "America/New_York"
    assert zone_for("Washington, d.c.") == "America/New_York"


@pytest.mark.asyncio
async def test_a_main_zone_is_used_but_not_stored(state):
    cfg = SimpleNamespace(compliance=None)
    pid = await seed_at(state, "Vancouver, Canada", "li@van.example")
    prospect = await state.get_prospect(pid)
    assert await recipient_zone(state, prospect, cfg) == "America/Toronto"
    assert (await state.get_prospect(pid)).timezone == ""
    # Coordinates cached later still correct it.
    await state.set_setting("geo:vancouver, canada", "49.28,-123.12")
    assert await recipient_zone(state, prospect, cfg) == "America/Vancouver"
    assert (await state.get_prospect(pid)).timezone == "America/Vancouver"


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


@pytest.mark.parametrize("location, country, point, zone", [
    # Coordinates settle a code that is a region and a country, either way.
    ("Jakarta, ID", "", "-6.21,106.85", "Asia/Jakarta"),
    ("Boise, ID", "", "43.62,-116.20", "America/Boise"),
    ("Mumbai, IN", "", "19.08,72.88", "Asia/Kolkata"),
    ("Tel Aviv, IL", "", "32.09,34.78", "Asia/Jerusalem"),
    ("St. John's, NL", "", "47.56,-52.71", "America/St_Johns"),
    ("Amsterdam, NL", "", "52.37,4.90", "Europe/Amsterdam"),
    ("Adelaide, SA", "", "-34.93,138.60", "Australia/Adelaide"),
    ("Riyadh, SA", "", "24.71,46.68", "Asia/Riyadh"),
    # So does a country from elsewhere (Precleared or prospect_country).
    ("Jakarta, ID", "ID", "", "Asia/Jakarta"),
    ("Berlin, DE", "GB", "", "Europe/London"),       # it beats the code's guess
    ("Mumbai, IN", "IN", "", "Asia/Kolkata"),
    ("Indianapolis, IN", "US", "", "America/Indiana/Indianapolis"),
    ("St. John's, NL", "CA", "", "America/St_Johns"),
    ("Adelaide, SA", "AU", "", "Australia/Adelaide"),
    ("Lima, PE", "PE", "", "America/Lima"),
    ("Charlottetown, PE", "CA", "", "America/Halifax"),
    # With nothing else, the reading stays as before: US state first.
    ("Tel Aviv, IL", "", "", "America/Chicago"),
    ("Amsterdam, NL", "", "", "Europe/Amsterdam"),
])
def test_code_that_is_a_region_and_a_country_is_settled_by_what_else_is_known(
        location, country, point, zone):
    assert zone_for(location, country=country, point=point) == zone


# ── The country, for Precleared ──

def test_country_for_names_a_country():
    assert country_for("Oslo, Norway") == ("NO", True)
    assert country_for("London, UK") == ("GB", True)
    assert country_for("Austin, TX") == ("US", True)
    assert country_for("Toronto, ON") == ("CA", True)
    assert country_for("Montréal, Québec") == ("CA", True)
    assert country_for("Sydney NSW 2000") == ("AU", True)
    assert country_for("Perth, WA, Australia", point="-31.95,115.86") == ("AU", True)
    assert country_for("United States") == ("US", True)


def test_country_for_treats_a_state_and_country_code_as_a_guess():
    # Delaware by convention, so not certain; Berlin's coordinates settle it.
    assert country_for("Berlin, DE") == ("US", False)
    assert country_for("Berlin, DE", point="52.52,13.40") == ("DE", True)
    assert country_for("Dublin, CA", point="37.70,-121.94") == ("US", True)
    assert country_for("Denver, CO") == ("US", False)       # CO is Colombia's code too
    assert country_for("Amsterdam, NL") == ("NL", False)    # or Newfoundland


def test_country_for_does_not_trust_coordinates_alone():
    # The state reading is vetoed and nothing names the country.
    assert country_for("Perth, WA", point="-31.95,115.86") == ("", False)
    assert country_for("", point="-31.95,115.86") == ("", False)
    # The nearest reference city to Seattle is Vancouver's: fine for the
    # clock, wrong for the law.
    assert country_for("Seattle", point="47.61,-122.33") == ("", False)


def test_country_for_has_nothing_for_a_bare_city():
    assert country_for("London") == ("", False)
    assert country_for("Remote") == ("", False)
    assert country_for("") == ("", False)


def test_zone_country():
    assert zone_country("Europe/London") == "GB"
    assert zone_country("America/Chicago") == "US"
    assert zone_country("Australia/Perth") == "AU"
    assert zone_country("Nowhere/Zone") == ""
    assert zone_country("") == ""
    # No zone is listed under two countries, so the inversion is exact.
    zones = [z for zones in pytz.country_timezones.values() for z in zones]
    assert len(zones) == len(set(zones))


def _icp(geography, coordinates=None):
    return SimpleNamespace(icp=SimpleNamespace(geography=geography, geo_coordinates=coordinates or {}))


def test_icp_country():
    assert icp_country(_icp(["Denver, Colorado", "Austin, TX"])) == "US"
    assert icp_country(_icp(["United States"])) == "US"
    assert icp_country(_icp(["Denver, CO", "Toronto, ON"])) == ""
    assert icp_country(_icp(["London"])) == ""
    assert icp_country(_icp(["Austin, TX", ""])) == ""
    assert icp_country(_icp([])) == ""
    assert icp_country(SimpleNamespace()) == ""
    # "CO" is Colombia's code too: a guess unless the coordinates settle it.
    assert icp_country(_icp(["Denver, CO"])) == ""
    assert icp_country(_icp(["Denver, CO"], {"Denver, CO": "39.7392,-104.9903,50"})) == "US"
    assert icp_country(SellbirdConfig(**{**BASE, "icp": {**BASE["icp"], "geography": ["Oslo, Norway"]}})) == "NO"


@pytest.mark.asyncio
async def test_recipient_country_does_not_promote_a_stored_zone_to_evidence(state):
    # Stored zones have no provenance: they can come from approximate
    # coordinates or a previous location, so they cannot settle the country.
    pid = await seed_at(state, "Berlin, DE", "lena@de-acme.example")
    assert await recipient_country(state, await state.get_prospect(pid)) == ""
    await state.set_prospect_timezone(pid, "Europe/Berlin")
    assert await recipient_country(state, await state.get_prospect(pid)) == ""
    assert await recipient_country(state, None) == ""


@pytest.mark.asyncio
async def test_recipient_country_reads_the_location_and_the_cached_coordinates(state):
    austin = await seed_at(state, "Austin, TX", "jane@tx-acme.com")
    berlin = await seed_at(state, "Berlin, DE", "lena@de-acme.example")
    nowhere = await seed_at(state, "", "sam@nowhere-acme.com")
    assert await recipient_country(state, await state.get_prospect(austin)) == "US"
    assert await recipient_country(state, await state.get_prospect(berlin)) == ""
    await state.set_setting("geo:berlin, de", "52.52,13.40")
    assert await recipient_country(state, await state.get_prospect(berlin)) == "DE"
    assert await recipient_country(state, await state.get_prospect(nowhere)) == ""


@pytest.mark.asyncio
async def test_a_zone_stored_from_coordinates_alone_gives_no_country(state):
    # "Seattle" with cached coordinates gets Vancouver's zone (the nearest
    # reference city, over the border): the right clock, so it is stored,
    # but not a country to tell Precleared.
    pid = await seed_at(state, "Seattle", "sam@sea.example")
    await state.set_setting("geo:seattle", "47.61,-122.33")
    cfg = SimpleNamespace(compliance=None)
    assert await recipient_zone(state, await state.get_prospect(pid), cfg) == "America/Vancouver"
    prospect = await state.get_prospect(pid)
    assert prospect.timezone == "America/Vancouver"
    assert await recipient_country(state, prospect) == ""
    # Losing the geocoding cache must not turn the approximate zone into fact.
    await state.set_setting("geo:seattle", "-")
    assert await recipient_country(state, prospect) == ""


def test_faraway_coordinates_do_not_settle_an_ambiguous_country_code():
    assert country_for("Example, IN", point="-33.87,151.21")[1] is False


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
    assert SellbirdConfig(**BASE).channels.email.send_window is None


def test_window_settings():
    w = SendWindowConfig()
    assert (w.start, w.end, w.weekdays_only, w.recipient_timezone) == ("08:00", "17:00", True, True)
    # YAML 1.1 turns an unquoted 17:00 into 1020.
    assert SendWindowConfig(start=480, end=1020).end == "17:00"
    with pytest.raises(ValidationError):
        SendWindowConfig(start="8am")
    with pytest.raises(ValidationError):
        SendWindowConfig(start="17:00", end="08:00")  # can't run past midnight
    cfg = SellbirdConfig(**BASE, channels={"email": {
        "provider": "gmail", "send_window": {"start": "09:00", "end": "16:30"}}})
    assert cfg.channels.email.send_window.end == "16:30"


def test_which_configs_send_through_quiet_hours():
    from sellbird.main import sends_through_quiet_hours

    def cfg(**email):
        return SellbirdConfig(**BASE, channels={"email": {"provider": "gmail", **email}})
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
async def test_precleared_country_wins_over_the_configured_prospect_country(state, monkeypatch):
    # prospect_country is the fallback for when nothing else places the
    # recipient: once Precleared says Germany, the window is Berlin's, not
    # the London one the configured default gave before the check ran.
    from sellbird.config import ComplianceConfig
    pid = await seed_at(state, "", "lena@de-acme.example")
    item = await approved(state, pid, "lena@de-acme.example")
    calls = []
    provider = FakeProvider()
    sender = window_sender(state, provider, monkeypatch, operator_zone="Asia/Tokyo")
    monkeypatch.setattr(sender.config, "compliance", ComplianceConfig(
        precleared={"prospect_country": "GB"}), raising=False)
    ok_de = {**OK_GB, "facts": {"recipient_country": "DE"}}
    sender.precleared = PreclearedClient(sender_country="GB", transport=precleared_mock(calls, ok_de))

    # 15:30 UTC on a Wednesday: 16:30 in London, 17:30 in Berlin.
    pin_now(monkeypatch, datetime(2026, 10, 21, 15, 30))
    await sender._drain_due()
    assert provider.sent == [] and len(calls) == 1
    assert (await state.get_prospect(pid)).timezone == "Europe/Berlin"
    moved = datetime.fromisoformat((await outbox_row(state, item))["send_at"])
    assert_office_hours(moved, "Europe/Berlin")


@pytest.mark.asyncio
async def test_precleared_country_settles_a_code_that_is_a_state_and_a_country(state, monkeypatch):
    # "Berlin, DE" reads as Delaware until something says Germany. Staging
    # mustn't store that reading for good, or Precleared's answer never counts.
    pid = await seed_at(state, "Berlin, DE", "lena@de-acme.example", status="new")
    campaign = await seed_campaign(state, [pid])
    calls = []
    provider = FakeProvider()
    sender = window_sender(state, provider, monkeypatch)
    ok_de = {**OK_GB, "facts": {"recipient_country": "DE"}}
    sender.precleared = PreclearedClient(sender_country="GB", transport=precleared_mock(calls, ok_de))

    # 15:30 UTC on a Wednesday: 11:30 in New York, 17:30 in Berlin.
    pin_now(monkeypatch, datetime(2026, 10, 21, 15, 30))
    await sender._stage_campaign_native(campaign)
    first = [r for r in await state.get_outbox(limit=10) if r["step"] == 1][0]
    await sender._drain_due()
    assert provider.sent == [] and len(calls) == 1
    assert (await state.get_prospect(pid)).timezone == "Europe/Berlin"
    moved = datetime.fromisoformat((await outbox_row(state, first["id"]))["send_at"])
    assert_office_hours(moved, "Europe/Berlin")


@pytest.mark.asyncio
async def test_a_state_code_that_is_also_a_country_still_gets_the_state_window(state, monkeypatch):
    # Without Precleared nothing settles "DE", so it isn't stored, but the
    # US reading still applies every time it's worked out.
    pid = await seed_at(state, "Wilmington, DE", "sam@de-state.example", status="new")
    campaign = await seed_campaign(state, [pid])
    pin_now(monkeypatch, WED_0200)
    await window_sender(state, FakeProvider(), monkeypatch)._stage_campaign_native(campaign)
    for row in await state.get_outbox(limit=10):
        assert_office_hours(datetime.fromisoformat(row["send_at"]), "America/New_York")
    assert (await state.get_prospect(pid)).timezone == ""


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


# ── The country Sellbird gives Precleared ──

def checked_sender(state, provider, calls, body=OK_GB, **client):
    """A sender without a window, so only the check matters, on a mocked Precleared."""
    sender = make_sender(state, provider, require_approval=False)
    sender.precleared = PreclearedClient(sender_country="GB", transport=precleared_mock(calls, body), **client)
    return sender


@pytest.mark.asyncio
async def test_sender_passes_the_company_country_to_precleared(state):
    pid = await seed_at(state, "Austin, TX", "jane@tx-acme.com")
    await approved(state, pid, "jane@tx-acme.com")
    calls, provider = [], FakeProvider()
    ok_us = {**OK_GB, "facts": {"recipient_country": "US"}}
    # Even with a default configured, a country Sellbird is sure of goes instead.
    await checked_sender(state, provider, calls, ok_us, prospect_country="GB")._drain_due()
    assert [m["to"] for m in provider.sent] == ["jane@tx-acme.com"]
    assert calls[0]["recipient_country"] == "US"
    assert "default_recipient_country" not in calls[0]


@pytest.mark.asyncio
async def test_sender_falls_back_to_prospect_country_as_the_default(state):
    pid = await seed_at(state, "", "kate@uk-acme.co.uk")
    await approved(state, pid, "kate@uk-acme.co.uk")
    calls = []
    await checked_sender(state, FakeProvider(), calls, prospect_country="GB")._drain_due()
    assert calls[0]["default_recipient_country"] == "GB"
    assert "recipient_country" not in calls[0]


@pytest.mark.asyncio
async def test_sender_sends_no_country_for_a_guessed_state_code(state):
    # "DE" is Delaware by convention and Germany by code: not sure, so nothing.
    pid = await seed_at(state, "Wilmington, DE", "sam@de-state.example")
    await approved(state, pid, "sam@de-state.example")
    calls = []
    await checked_sender(state, FakeProvider(), calls)._drain_due()
    assert len(calls) == 1
    assert "recipient_country" not in calls[0] and "default_recipient_country" not in calls[0]


@pytest.mark.asyncio
async def test_sender_settles_a_state_code_by_the_cached_coordinates(state):
    pid = await seed_at(state, "Berlin, DE", "lena@de-acme.example")
    await approved(state, pid, "lena@de-acme.example")
    await state.set_setting("geo:berlin, de", "52.52,13.40")
    calls = []
    ok_de = {**OK_GB, "facts": {"recipient_country": "DE"}}
    await checked_sender(state, FakeProvider(), calls, ok_de)._drain_due()
    assert calls[0]["recipient_country"] == "DE"


@pytest.mark.asyncio
async def test_country_lookup_failure_does_not_stop_the_drain(state, monkeypatch):
    pid = await seed_at(state, "Austin, TX", "jane@tx-acme.com")
    await approved(state, pid, "jane@tx-acme.com")
    calls, provider = [], FakeProvider()
    sender = checked_sender(state, provider, calls)

    async def boom(state, prospect):
        raise RuntimeError("country lookup failed")
    monkeypatch.setattr(sender_mod, "recipient_country", boom)
    await sender._drain_due()
    # The check still runs, without a country, and the email still sends.
    assert [m["to"] for m in provider.sent] == ["jane@tx-acme.com"]
    assert len(calls) == 1 and "recipient_country" not in calls[0]


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
    import sellbird.agents.analyst
    import sellbird.agents.handler
    import sellbird.agents.scout
    import sellbird.agents.writer
    import sellbird.main as main_mod

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

            async def cold_due_in_window(self):  # the real check has its own tests
                return bool(await state.get_outbox(status="approved", limit=1))
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
    monkeypatch.setattr(sellbird.agents.scout, "Scout", agent("prospect"))
    monkeypatch.setattr(sellbird.agents.writer, "Writer", agent("write"))
    monkeypatch.setattr(sender_mod, "Sender", agent("send"))
    monkeypatch.setattr(sellbird.agents.handler, "Handler", agent("replies"))
    monkeypatch.setattr(sellbird.agents.analyst, "Analyst", agent("analyze"))
    return main_mod


@pytest.mark.asyncio
async def test_quiet_hours_read_replies_then_send_what_is_due(state, monkeypatch):
    cfg = SellbirdConfig(**BASE, channels={"email": {"provider": "gmail", "send_window": {}}})
    pid = await seed_at(state, "Tokyo, Japan", "aiko@tokyo-acme.jp")
    await approved(state, pid, "aiko@tokyo-acme.jp")
    calls, stop = [], asyncio.Event()
    main_mod = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == ["replies", "send"]  # no prospecting or writing


@pytest.mark.asyncio
async def test_quiet_hours_with_nothing_due_do_nothing(state, monkeypatch):
    cfg = SellbirdConfig(**BASE, channels={"email": {"provider": "gmail", "send_window": {}}})
    calls, stop = [], asyncio.Event()
    main_mod = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == []


@pytest.mark.asyncio
async def test_without_recipient_zones_quiet_hours_hold_everything(state, monkeypatch):
    cfg = SellbirdConfig(**BASE, channels={"email": {"provider": "gmail"}})
    pid = await seed_at(state, "Tokyo, Japan", "aiko@tokyo-acme.jp")
    await approved(state, pid, "aiko@tokyo-acme.jp")
    calls, stop = [], asyncio.Event()
    main_mod = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == []


def _patch_heartbeat_real_sender(monkeypatch, cfg, state, calls, stop, provider):
    """The heartbeat with the real Sender on a FakeProvider; other agents stubbed."""
    real_sender = sender_mod.Sender
    main_mod = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    monkeypatch.setattr(sender_mod, "Sender", real_sender)
    monkeypatch.setattr(sender_mod, "get_mail_provider", lambda c, e: provider)
    monkeypatch.setattr(sender_mod, "SEND_JITTER_SECONDS", (0, 0))
    monkeypatch.setattr(main_mod, "load_env", lambda: SimpleNamespace(instantly_api_key=""))
    return main_mod


def _quiet_cfg(**email):
    return SellbirdConfig(**BASE, channels={"email": {
        "provider": "gmail", "require_approval": False, "send_window": {}, **email}})


@pytest.mark.asyncio
async def test_quiet_hours_send_cold_email_in_its_window_but_hold_replies(state, monkeypatch):
    tokyo = await seed_at(state, "Tokyo, Japan", "aiko@tokyo.example")
    asked = await seed_at(state, "New York, NY", "bob@ny-asked.example", status="replied")
    await approved(state, tokyo, "aiko@tokyo.example")
    reply = await approved(state, asked, "bob@ny-asked.example", kind="reply")
    provider, calls, stop = FakeProvider(), [], asyncio.Event()
    main_mod = _patch_heartbeat_real_sender(monkeypatch, _quiet_cfg(), state, calls, stop, provider)
    pin_now(monkeypatch, WED_0200)  # 11:00 in Tokyo
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)

    assert [m["to"] for m in provider.sent] == ["aiko@tokyo.example"]
    assert (await outbox_row(state, reply))["status"] == "approved"  # waits for your hours


@pytest.mark.asyncio
@pytest.mark.parametrize("held_by", ["reply_only", "outside_window", "paused", "daily_cap"])
async def test_quiet_hours_dont_wake_for_mail_that_cannot_go(state, monkeypatch, held_by):
    tokyo = await seed_at(state, "Tokyo, Japan", "aiko@tokyo.example")
    ny = await seed_at(state, "New York, NY", "jane@ny.example", status="replied")
    email = {}
    if held_by == "reply_only":
        await approved(state, ny, "jane@ny.example", kind="reply")
    elif held_by == "outside_window":
        await state.update_prospect_status(ny, "queued")
        await approved(state, ny, "jane@ny.example")  # 22:00 in New York
    else:
        await approved(state, tokyo, "aiko@tokyo.example")
    if held_by == "paused":
        await state.set_setting(sender_mod.KILL_SWITCH_KEY, "test pause")
    if held_by == "daily_cap":
        email = {"max_daily_sends": 1}
        done = await approved(state, ny, "jane@ny.example", kind="reply")
        await state.update_outbox_item(done, status="sent", sent_at=_utc_now_iso())
    provider, calls, stop = FakeProvider(), [], asyncio.Event()
    main_mod = _patch_heartbeat_real_sender(
        monkeypatch, _quiet_cfg(**email), state, calls, stop, provider)
    pin_now(monkeypatch, WED_0200)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == [] and provider.sent == []  # no reading mail, no Claude


@pytest.mark.asyncio
async def test_quiet_hours_dont_wake_after_a_compliance_hold(state, monkeypatch):
    pid = await seed_at(state, "Tokyo, Japan", "aiko@tokyo.example")
    await approved(state, pid, "aiko@tokyo.example")
    provider = FakeProvider()
    sender = window_sender(state, provider, monkeypatch)
    down = httpx.MockTransport(lambda request: httpx.Response(503))
    sender.precleared = PreclearedClient(sender_country="GB", transport=down)
    pin_now(monkeypatch, WED_0200)  # 11:00 in Tokyo
    assert await sender.cold_due_in_window()
    await sender._drain_due()
    assert provider.sent == []
    assert not await sender.cold_due_in_window()  # until a drain outside quiet hours


@pytest.mark.asyncio
async def test_a_zone_that_cannot_be_worked_out_doesnt_stop_the_drain(state, monkeypatch):
    broken = await seed_at(state, "Tokyo, Japan", "aiko@tokyo.example")
    asked = await seed_at(state, "New York, NY", "bob@ny-asked.example", status="replied")
    await approved(state, broken, "aiko@tokyo.example")
    await approved(state, asked, "bob@ny-asked.example", kind="reply")
    provider = FakeProvider()
    sender = window_sender(state, provider, monkeypatch, operator_zone="Asia/Tokyo")

    async def boom(prospect, country="", given=""):
        raise RuntimeError("zone lookup failed")
    monkeypatch.setattr(sender, "_send_zone", boom)
    pin_now(monkeypatch, WED_0200)  # 11:00 in Tokyo, the fallback zone too
    await sender._drain_due()
    assert sorted(m["to"] for m in provider.sent) == ["aiko@tokyo.example", "bob@ny-asked.example"]


@pytest.mark.asyncio
async def test_quiet_hours_send_cold_email_queued_behind_many_replies(state, monkeypatch):
    tokyo = await seed_at(state, "Tokyo, Japan", "aiko@tokyo.example")
    cold = await approved(state, tokyo, "aiko@tokyo.example")
    earlier = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
    for n in range(30):
        pid = await seed_at(state, "New York, NY", f"r{n}@replies{n}.example", status="replied")
        item = await approved(state, pid, f"r{n}@replies{n}.example", kind="reply")
        await state.update_outbox_item(item, send_at=earlier)
    provider = FakeProvider()
    sender = window_sender(state, provider, monkeypatch)
    sender.cold_only = True
    pin_now(monkeypatch, WED_0200)  # 11:00 in Tokyo
    assert await sender.cold_due_in_window()
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["aiko@tokyo.example"]
    assert (await outbox_row(state, cold))["status"] == "sent"


@pytest.mark.asyncio
async def test_quiet_hours_dont_wake_under_the_warmup_cap(state, monkeypatch):
    from sellbird.config import WarmupConfig
    pid = await seed_at(state, "Tokyo, Japan", "aiko@tokyo.example")
    await approved(state, pid, "aiko@tokyo.example")
    sender = window_sender(state, FakeProvider(), monkeypatch)
    pin_now(monkeypatch, WED_0200)
    assert await sender.cold_due_in_window()
    monkeypatch.setattr(sender.config.channels.email, "warmup",
                        WarmupConfig(start_date=date.today().isoformat()), raising=False)
    assert not await sender.cold_due_in_window()  # day one: warmup only, no cold email


@pytest.mark.asyncio
async def test_precleared_main_zone_is_used_on_every_check(state, monkeypatch):
    # Precleared says Australia, which gives a main zone that isn't stored.
    # Every later check must still use it, not the operator's zone: the two
    # windows don't overlap, so needing both would hold the email for good.
    pid = await seed_at(state, "", "mia@au-acme.example")
    item = await approved(state, pid, "mia@au-acme.example")
    calls, provider = [], FakeProvider()
    sender = window_sender(state, provider, monkeypatch, operator_zone="America/New_York")
    ok_au = {**OK_GB, "facts": {"recipient_country": "AU"}}
    sender.precleared = PreclearedClient(sender_country="GB", transport=precleared_mock(calls, ok_au))
    pin_now(monkeypatch, datetime(2026, 10, 21, 14, 0))  # 10:00 New York, 01:00 Sydney
    await sender._drain_due()
    assert provider.sent == [] and len(calls) == 1
    moved = datetime.fromisoformat((await outbox_row(state, item))["send_at"])
    assert_office_hours(moved, "Australia/Sydney")
    pin_now(monkeypatch, moved + timedelta(minutes=1))
    await state.update_outbox_item(item, send_at=(moved - timedelta(days=3)).isoformat())
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["mia@au-acme.example"]


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


# ── Storage ──

@pytest.mark.asyncio
async def test_existing_prospects_get_an_empty_zone_on_upgrade(tmp_path):
    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:13]:  # a database from before v14
            await db.executescript(script)
        await db.execute("PRAGMA user_version = 13")
        await db.execute("INSERT INTO prospects (id, email, first_name) VALUES ('p1', 'a@b.co', 'A')")
        await db.commit()
    sm = StateManager(db_path)
    await sm.init_db()
    assert (await sm.get_prospect("p1")).timezone == ""
    await sm.set_prospect_timezone("p1", "Europe/Oslo")
    assert (await sm.get_prospect("p1")).timezone == "Europe/Oslo"
