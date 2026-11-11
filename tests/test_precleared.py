"""Tests for the optional Precleared check in the native send path: off by
default, one check per address, blocked verdicts fail the outbox item, an
unavailable check holds cold email without failing it, replies are never
checked. No network: every Precleared call goes to an httpx.MockTransport."""

import json
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from sellbird.config import PRECLEARED_SENDER_COUNTRIES, PreclearedConfig
from sellbird.integrations.precleared import SENDER_COUNTRIES, SETTINGS_PREFIX, PreclearedClient
from sellbird.models.company import Company
from sellbird.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from sellbird.state import StateManager
from tests.test_outbox_native import (
    Cfg,
    Env,
    FakeProvider,
    _now_iso,
    make_sender,
    seed_prospect,
)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


OK_BODY = {
    "verdict": "ok_to_email",
    "confidence": "high",
    "reasons": [{
        "rule_key": "gb_corporate_ok",
        "text": "Corporate subscribers need no prior consent.",
        "source_title": "PECR reg. 22",
    }],
    "next_step": "Send, with a footer that says who you are and how to opt out.",
    "receipt": {"id": "cc_rcpt_OK0000000001"},
}

CONSENT_BODY = {
    "verdict": "consent_needed",
    "confidence": "high",
    "reasons": [{
        "rule_key": "gb_individual_consent",
        "text": "Sole traders count as individuals, so consent is needed.",
        "source_title": "PECR reg. 22(2)",
    }],
    "next_step": "Do not send cold email. Get consent first.",
    "receipt": {"id": "cc_rcpt_NO0000000001"},
}


def mock_precleared(calls, body=None, status=200):
    """A Precleared stand-in that records each request."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"json": json.loads(request.content), "headers": dict(request.headers)})
        if status != 200:
            return httpx.Response(status, json={"error": "daily_limit", "message": "Limit reached."})
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


def with_check(sender, transport, api_key=""):
    sender.precleared = PreclearedClient(sender_country="GB", api_key=api_key, transport=transport)
    return sender


async def queue(state, pid, step=1, kind="sequence", to="jane@acme.com", campaign="c1"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="hello",
        body="Fine body. Question?", send_at=_now_iso(), status="approved",
        campaign_id=campaign if kind == "sequence" else "", step=step, kind=kind,
    )


async def days_pass(state, days=4):
    """Move every sent email back in time, so a follow-up's gap has passed."""
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE outbox SET sent_at = datetime(sent_at, ?) "
                         "WHERE status = 'sent'", (f"-{days} days",))
        await db.commit()


async def logged_receipts(state):
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(
            "SELECT details_json FROM actions WHERE action_type = 'email_sent'"
        ) as cursor:
            rows = await cursor.fetchall()
    return [json.loads(r[0]).get("precleared_receipt", "") for r in rows]


# ── off by default ──


def test_check_is_off_unless_enabled():
    assert PreclearedClient.from_config(Cfg(), Env()) is None
    off = SimpleNamespace(compliance=SimpleNamespace(precleared=PreclearedConfig()))
    assert PreclearedClient.from_config(off, Env()) is None


@pytest.mark.asyncio
async def test_disabled_sender_sends_without_a_check(state):
    pid = await seed_prospect(state)
    await queue(state, pid)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    assert sender.precleared is None
    await sender._drain_due()
    assert len(provider.sent) == 1


# ── config ──


def test_config_needs_a_sender_country_when_enabled():
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True)
    cfg = PreclearedConfig(enabled=True, sender_country="gb")
    assert cfg.sender_country == "GB"
    assert cfg.address_source == "unknown"
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True, sender_country="US", address_source="guessed")


def test_config_takes_a_sender_in_any_of_precleared_s_fourteen_countries():
    assert PRECLEARED_SENDER_COUNTRIES == (
        "GB", "US", "CA", "DE", "FR", "AU", "IE", "NL", "ES", "IT", "LV", "AE", "EG", "QA")
    assert SENDER_COUNTRIES == PRECLEARED_SENDER_COUNTRIES
    for code in PRECLEARED_SENDER_COUNTRIES:
        assert PreclearedConfig(enabled=True, sender_country=code.lower()).sender_country == code
    # Common spellings are read as their codes.
    assert PreclearedConfig(enabled=True, sender_country="UK").sender_country == "GB"
    assert PreclearedConfig(enabled=True, sender_country="usa").sender_country == "US"
    assert PreclearedConfig(enabled=True, sender_country=" uae ").sender_country == "AE"
    # A country Precleared doesn't cover is still refused when the check is on.
    with pytest.raises(ValidationError, match="must be one of GB, US, CA, DE"):
        PreclearedConfig(enabled=True, sender_country="BR")
    # Off, the setting isn't checked, so an old config still loads.
    assert PreclearedConfig(enabled=False, sender_country="BR").sender_country == "BR"


def test_from_config_reads_settings_key_and_pitch():
    config = SimpleNamespace(
        compliance=SimpleNamespace(precleared=PreclearedConfig(
            enabled=True, sender_country="US", address_source="published",
        )),
        product=SimpleNamespace(description="Route planning for delivery firms"),
    )
    client = PreclearedClient.from_config(config, SimpleNamespace(precleared_api_key="pc_live_test"))
    assert client.sender_country == "US"
    assert client.address_source == "published"
    assert client.api_key == "pc_live_test"
    assert client.pitch == "Route planning for delivery firms"


def test_prospect_country_is_optional_and_normalised():
    assert PreclearedConfig(enabled=True, sender_country="US").prospect_country == ""
    cfg = PreclearedConfig(enabled=True, sender_country="US", prospect_country=" us ")
    assert cfg.prospect_country == "US"
    cfg = PreclearedConfig(enabled=True, sender_country="GB", prospect_country="uk")
    assert cfg.prospect_country == "GB"
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True, sender_country="US", prospect_country="United States")


@pytest.mark.parametrize("yaml_value", [False, True])
def test_unquoted_yaml_country_is_an_error_not_ignored(yaml_value):
    # YAML reads an unquoted NO (Norway) as False and ON as True.
    with pytest.raises(ValidationError, match="quote"):
        PreclearedConfig(enabled=True, sender_country="US", prospect_country=yaml_value)


def test_client_normalises_prospect_country_like_the_config():
    assert PreclearedClient("US", prospect_country="uk").prospect_country == "GB"
    assert PreclearedClient("US", prospect_country="usa").prospect_country == "US"
    with pytest.raises(ValueError):
        PreclearedClient("US", prospect_country="garbage!!")


def test_from_config_reads_prospect_country():
    config = SimpleNamespace(
        compliance=SimpleNamespace(precleared=PreclearedConfig(
            enabled=True, sender_country="US", prospect_country="US",
        )),
        product=SimpleNamespace(description=""),
    )
    client = PreclearedClient.from_config(config, SimpleNamespace(precleared_api_key=""))
    assert client.prospect_country == "US"


@pytest.mark.asyncio
async def test_prospect_country_is_sent_as_a_fallback_only_when_set(state):
    calls = []
    without = PreclearedClient("US", transport=mock_precleared(calls, OK_BODY))
    await without.clear("jane@acme.com", state)
    assert "default_recipient_country" not in calls[0]["json"]
    assert "recipient_country" not in calls[0]["json"]

    with_country = PreclearedClient(
        "US", prospect_country="US", transport=mock_precleared(calls, OK_BODY),
    )
    await with_country.clear("john@acme.com", state)
    assert calls[1]["json"]["default_recipient_country"] == "US"
    assert "recipient_country" not in calls[1]["json"]


@pytest.mark.asyncio
async def test_setting_prospect_country_forces_a_fresh_check_then_caches(state):
    # A verdict cached before the setting existed was decided without the
    # fallback country, so it must not be reused once the setting is on.
    calls = []
    await PreclearedClient("US", transport=mock_precleared(calls, OK_BODY)).clear("jane@acme.com", state)
    with_country = PreclearedClient(
        "US", prospect_country="US", transport=mock_precleared(calls, OK_BODY),
    )
    await with_country.clear("jane@acme.com", state)
    await with_country.clear("jane@acme.com", state)
    assert len(calls) == 2
    assert calls[1]["json"]["default_recipient_country"] == "US"


def test_prospect_country_loads_from_sellbird_yaml(tmp_path):
    import yaml

    from sellbird.config import load_config

    path = tmp_path / "sellbird.yaml"
    path.write_text(yaml.safe_dump({
        "persona": {"name": "H", "company": "A", "role": "B",
                    "email": "h@a.com", "linkedin": "", "tone": "casual"},
        "product": {"name": "P", "description": "D", "pricing": "$1",
                    "key_benefits": ["x"], "objection_responses": {}},
        "icp": {"industries": ["SaaS"], "company_size": "10-50",
                "titles": ["CEO"], "geography": ["US"]},
        "compliance": {"precleared": {
            "enabled": True, "sender_country": "US", "prospect_country": "usa",
        }},
    }))
    assert load_config(str(path)).compliance.precleared.prospect_country == "US"


def test_prospect_country_changes_the_cache_fingerprint_only_when_set():
    base = PreclearedClient("US")._fingerprint()
    assert PreclearedClient("US", prospect_country="")._fingerprint() == base
    assert PreclearedClient("US", prospect_country="US")._fingerprint() != base


# ── the recipient's country ──


FOUND_GB = {**OK_BODY, "facts": {"recipient_country": "GB", "country_source": "domain"}}


@pytest.mark.asyncio
async def test_confident_country_is_sent_as_recipient_country(state):
    calls = []
    client = PreclearedClient("GB", prospect_country="GB", transport=mock_precleared(calls, OK_BODY))
    await client.clear("jane@acme.com", state, country="US")
    assert calls[0]["json"]["recipient_country"] == "US"
    # Never both: the default is for when nobody knows the country.
    assert "default_recipient_country" not in calls[0]["json"]
    await client.clear("john@acme.com", state, country="us")
    assert calls[1]["json"]["recipient_country"] == "US"


def _config(geography, prospect_country=""):
    return SimpleNamespace(
        compliance=SimpleNamespace(precleared=PreclearedConfig(
            enabled=True, sender_country="US", prospect_country=prospect_country)),
        product=SimpleNamespace(description=""),
        icp=SimpleNamespace(geography=geography, geo_coordinates={}),
    )


def test_icp_country_is_the_default_when_prospect_country_is_unset():
    env = SimpleNamespace(precleared_api_key="")
    one = PreclearedClient.from_config(_config(["Austin, TX", "Denver, Colorado"]), env)
    assert one.default_country == "US" and one.prospect_country == ""
    assert PreclearedClient.from_config(_config(["Austin, TX", "Toronto, ON"]), env).default_country == ""
    assert PreclearedClient.from_config(_config([]), env).default_country == ""
    # prospect_country wins when both exist.
    both = PreclearedClient.from_config(_config(["Austin, TX"], prospect_country="GB"), env)
    assert both.prospect_country == "GB" and both.default_country == "GB"
    # Built directly (the tests' configs have no ICP), the same normalising.
    assert PreclearedClient("US", default_country="uk").default_country == "GB"
    assert PreclearedClient("US", prospect_country="GB", default_country="US").default_country == "GB"


@pytest.mark.asyncio
async def test_icp_country_is_sent_as_the_default(state):
    calls = []
    client = PreclearedClient("US", default_country="US", transport=mock_precleared(calls, OK_BODY))
    await client.clear("jane@acme.com", state)
    assert calls[0]["json"]["default_recipient_country"] == "US"
    assert "recipient_country" not in calls[0]["json"]
    await client.clear("john@acme.com", state, country="GB")
    assert calls[1]["json"]["recipient_country"] == "GB"
    assert "default_recipient_country" not in calls[1]["json"]


def test_country_changes_the_fingerprint_only_when_set():
    client = PreclearedClient("US")
    base = client._fingerprint()
    assert client._fingerprint("") == base
    assert client._fingerprint("GB") != base
    assert client._fingerprint("GB") != client._fingerprint("US")
    # A default is no part of the question once the country is known.
    assert PreclearedClient("US", prospect_country="GB")._fingerprint("US") == client._fingerprint("US")
    # The ICP's country counts only while it stands in for prospect_country,
    # so verdicts cached under prospect_country are untouched by it.
    assert PreclearedClient("US", default_country="US")._fingerprint() != base
    assert (PreclearedClient("US", prospect_country="GB", default_country="US")._fingerprint()
            == PreclearedClient("US", prospect_country="GB")._fingerprint())


@pytest.mark.asyncio
async def test_a_verdict_cached_without_a_country_is_kept_when_there_is_still_none(state):
    calls = []
    client = PreclearedClient("US", transport=mock_precleared(calls, OK_BODY))
    await client.clear("jane@acme.com", state)
    await client.clear("jane@acme.com", state)
    assert (await client.clear("jane@acme.com", state, country="")).cached
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_new_confident_country_forces_one_fresh_check_then_caches(state):
    # OK_BODY names no country, so a country now known is a new question:
    # one more check, then cached again.
    calls = []
    client = PreclearedClient("US", transport=mock_precleared(calls, OK_BODY))
    await client.clear("jane@acme.com", state)
    await client.clear("jane@acme.com", state, country="GB")
    await client.clear("jane@acme.com", state, country="GB")
    assert len(calls) == 2
    assert calls[1]["json"]["recipient_country"] == "GB"


@pytest.mark.asyncio
async def test_a_verdict_precleared_decided_for_the_same_country_is_kept(state):
    # Cached before Sellbird sent countries, with the one Precleared found
    # itself: Sellbird now knowing the same one asks the same question.
    calls = []
    client = PreclearedClient("US", transport=mock_precleared(calls, FOUND_GB))
    await client.clear("jane@acme.com", state)
    again = await client.clear("jane@acme.com", state, country="GB")
    assert again.cached and again.country == "GB" and len(calls) == 1
    assert await client.known_country("jane@acme.com", state, "GB") == "GB"
    # A different country is a different question.
    await client.clear("jane@acme.com", state, country="IE")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_known_country_uses_the_same_fingerprint_as_clear(state):
    calls = []
    found_us = {**OK_BODY, "facts": {"recipient_country": "US"}}
    client = PreclearedClient("GB", transport=mock_precleared(calls, found_us))
    await client.clear("jane@acme.com", state, country="US")
    assert await client.known_country("jane@acme.com", state, "US") == "US"
    assert await client.known_country("jane@acme.com", state) == ""   # a different question
    assert await client.known_country("jane@acme.com", state, "GB") == ""
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["GBR", "g b", None, 12, "G1", " "])
async def test_bad_country_values_are_dropped_not_raised(state, bad):
    calls = []
    client = PreclearedClient("US", transport=mock_precleared(calls, FOUND_GB))
    assert (await client.clear("jane@acme.com", state, country=bad)).allowed
    assert "recipient_country" not in calls[0]["json"]
    # Treated as no country throughout, so the cached verdict is found.
    assert await client.known_country("jane@acme.com", state, bad) == "GB"


# ── the company's and person's names ──


@pytest.mark.asyncio
async def test_names_are_sent_when_sellbird_has_them(state):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, OK_BODY))
    await client.clear("jane@acme.com", state, company="Acme Widgets Ltd", name="Jane Doe")
    assert calls[0]["json"]["company_name"] == "Acme Widgets Ltd"
    assert calls[0]["json"]["recipient_name"] == "Jane Doe"
    await client.clear("john@acme.com", state)
    assert "company_name" not in calls[1]["json"] and "recipient_name" not in calls[1]["json"]


@pytest.mark.asyncio
async def test_names_are_tidied_and_cut_to_precleareds_limits(state):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, OK_BODY))
    await client.clear("a@acme.com", state, company="  Acme \n Widgets\tLtd ", name=" Jane   Doe ")
    assert calls[0]["json"]["company_name"] == "Acme Widgets Ltd"
    assert calls[0]["json"]["recipient_name"] == "Jane Doe"
    await client.clear("b@acme.com", state, company="C" * 300, name="N" * 300)
    assert calls[1]["json"]["company_name"] == "C" * 160
    assert calls[1]["json"]["recipient_name"] == "N" * 120


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, 12, "", "   ", ["Acme"]])
async def test_empty_or_odd_names_are_left_out(state, bad):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, OK_BODY))
    assert (await client.clear("jane@acme.com", state, company=bad, name=bad)).allowed
    assert "company_name" not in calls[0]["json"] and "recipient_name" not in calls[0]["json"]


def test_names_change_the_fingerprint_only_when_set():
    client = PreclearedClient("GB")
    base = client._fingerprint()
    assert client._fingerprint("", "", "") == base
    assert client._fingerprint("", "Acme") != base
    assert client._fingerprint("", "", "Jane Doe") != base
    assert client._fingerprint("", "Acme", "Jane Doe") != client._fingerprint("", "Acme", "John Roe")
    assert client._fingerprint("GB", "Acme") != client._fingerprint("", "Acme")


@pytest.mark.asyncio
async def test_a_cached_ok_without_names_stands_when_the_names_arrive(state):
    # The names only help Precleared find the company, so an answer that
    # already allows the email isn't worth a second check.
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, OK_BODY))
    await client.clear("jane@acme.com", state)
    again = await client.clear("jane@acme.com", state, company="Acme", name="Jane Doe")
    assert again.allowed and again.cached and len(calls) == 1


@pytest.mark.asyncio
async def test_a_cached_hold_without_names_is_asked_again_once_with_them(state):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, CONSENT_BODY))
    await client.clear("jane@acme.com", state)
    await client.clear("jane@acme.com", state, company="Acme", name="Jane Doe")
    assert len(calls) == 2
    assert calls[1]["json"]["company_name"] == "Acme"
    assert calls[1]["json"]["recipient_name"] == "Jane Doe"
    # Then cached under the names, so the rest of the sequence costs nothing.
    assert (await client.clear("jane@acme.com", state, company="Acme", name="Jane Doe")).cached
    assert len(calls) == 2
    # Different names are a different question.
    await client.clear("jane@acme.com", state, company="Acme", name="John Roe")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_a_verdict_with_names_keeps_the_country_precleared_found(state):
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, FOUND_GB))
    await client.clear("jane@acme.com", state, company="Acme", name="Jane Doe")
    assert await client.known_country("jane@acme.com", state, company="Acme", name="Jane Doe") == "GB"
    # Sellbird later sure of the same country: the same question, still cached.
    again = await client.clear("jane@acme.com", state, country="GB", company="Acme", name="Jane Doe")
    assert again.cached and len(calls) == 1


async def seed_named(state, *, company_record="", company_field="", first="Sam", last="Lee",
                     source="", email="sam@acme-widgets.example"):
    company_id = ""
    if company_record:
        company_id = await state.add_company(Company(
            name=company_record, domain=email.split("@")[1]))
    return await state.add_prospect(Prospect(
        company_id=company_id, first_name=first, last_name=last, title="Owner",
        company=company_field, email=email, email_status="verified",
        email_verified=True, source=source))


async def checked_send(state, pid, to="sam@acme-widgets.example"):
    await queue(state, pid, to=to)
    calls, provider = [], FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))
    await sender._drain_due()
    return calls, provider


@pytest.mark.asyncio
async def test_sender_sends_the_company_records_name_and_the_person(state):
    pid = await seed_named(state, company_record="Acme Widgets Ltd",
                           company_field="acme-widgets.example")
    calls, provider = await checked_send(state, pid)
    assert len(provider.sent) == 1
    assert calls[0]["json"]["company_name"] == "Acme Widgets Ltd"
    assert calls[0]["json"]["recipient_name"] == "Sam Lee"


@pytest.mark.asyncio
async def test_sender_falls_back_to_the_prospects_company_field(state):
    pid = await seed_named(state, company_field="Acme Widgets")
    calls, _ = await checked_send(state, pid)
    assert calls[0]["json"]["company_name"] == "Acme Widgets"


@pytest.mark.asyncio
async def test_sender_sends_no_person_for_a_company_inbox(state):
    pid = await seed_named(state, company_field="Acme Widgets", first="", last="",
                           source=COMPANY_INBOX_SOURCE, email="hello@acme-widgets.example")
    calls, _ = await checked_send(state, pid, to="hello@acme-widgets.example")
    assert len(calls) == 1
    assert calls[0]["json"]["company_name"] == "Acme Widgets"
    assert "recipient_name" not in calls[0]["json"]


@pytest.mark.asyncio
async def test_a_company_lookup_failure_does_not_stop_the_drain(state, monkeypatch):
    pid = await seed_named(state, company_record="Acme Widgets Ltd", company_field="Acme Widgets")

    async def boom(company_id):
        raise RuntimeError("company lookup failed")
    monkeypatch.setattr(state, "get_company", boom)
    calls, provider = await checked_send(state, pid)
    assert len(provider.sent) == 1
    # The prospect's own field stands in for the record it couldn't load.
    assert calls[0]["json"]["company_name"] == "Acme Widgets"


# ── verdicts ──


@pytest.mark.asyncio
async def test_ok_verdict_sends_and_logs_the_receipt(state):
    pid = await seed_prospect(state)
    await queue(state, pid)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await sender._drain_due()

    assert len(provider.sent) == 1
    assert calls[0]["json"] == {
        "recipient_email": "jane@acme.com",
        "sender_country": "GB",
        "address_source": "unknown",
        "existing_customer": False,
        "company_name": "Acme",
        "recipient_name": "Jane Doe",
    }
    assert "authorization" not in calls[0]["headers"]
    assert await logged_receipts(state) == ["cc_rcpt_OK0000000001"]


@pytest.mark.asyncio
async def test_api_key_is_sent_as_bearer(state):
    pid = await seed_prospect(state)
    await queue(state, pid)
    calls = []
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_precleared(calls, OK_BODY), api_key="pc_live_test")
    await sender._drain_due()
    assert calls[0]["headers"]["authorization"] == "Bearer pc_live_test"


@pytest.mark.asyncio
async def test_consent_needed_fails_the_item_with_the_reason(state):
    pid = await seed_prospect(state)
    await queue(state, pid)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await sender._drain_due()

    assert provider.sent == []
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1
    assert failed[0]["error"].startswith("precleared: consent_needed")
    assert "PECR reg. 22(2)" in failed[0]["error"]
    assert "Get consent first" in failed[0]["error"]


@pytest.mark.asyncio
async def test_one_check_per_address(state):
    pid = await seed_prospect(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await queue(state, pid, step=1)
    await sender._drain_due()
    await days_pass(state)
    await queue(state, pid, step=2)
    await sender._drain_due()

    assert len(provider.sent) == 2
    assert len(calls) == 1
    assert await state.get_setting(SETTINGS_PREFIX + "jane@acme.com")


@pytest.mark.asyncio
async def test_blocked_verdict_is_remembered_for_later_steps(state):
    pid = await seed_prospect(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await queue(state, pid, step=1)
    await sender._drain_due()
    # Another campaign's first email to the same address: no second check.
    await queue(state, pid, step=1, campaign="c2")
    await sender._drain_due()

    assert provider.sent == []
    assert len(await state.get_outbox(status="failed")) == 2
    assert len(calls) == 1


# ── check unavailable ──


@pytest.mark.asyncio
async def test_daily_limit_holds_cold_email_but_replies_still_go(state):
    pid = await seed_prospect(state)
    other = await seed_prospect(state, email="sam@beta.com")
    await queue(state, pid)
    await queue(state, other, to="sam@beta.com")
    reply_pid = await seed_prospect(state, email="ann@gamma.com", status="replied")
    await queue(state, reply_pid, kind="reply", to="ann@gamma.com")
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, status=429))

    await sender._drain_due()

    # One failed call pauses the check for the rest of the cycle.
    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == ["ann@gamma.com"]
    assert await state.get_outbox(status="failed") == []
    waiting = await state.get_outbox(status="approved")
    assert sorted(i["to_email"] for i in waiting) == ["jane@acme.com", "sam@beta.com"]
    # Nothing cached, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + "jane@acme.com") == ""


@pytest.mark.asyncio
async def test_network_error_holds_the_email(state):
    pid = await seed_prospect(state)
    await queue(state, pid)

    def boom(request):
        raise httpx.ConnectError("no route to host")

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(boom))
    await sender._drain_due()

    assert provider.sent == []
    assert await state.get_outbox(status="failed") == []
    assert len(await state.get_outbox(status="approved")) == 1


@pytest.mark.asyncio
async def test_replies_are_never_checked(state):
    pid = await seed_prospect(state, status="replied")
    await queue(state, pid, kind="reply")
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await sender._drain_due()

    assert calls == []
    assert len(provider.sent) == 1


# ── review regressions (PR #8) ──


@pytest.mark.asyncio
async def test_changed_decision_inputs_require_a_new_check(state):
    calls = []
    initial = PreclearedClient("US", address_source="published", pitch="Delivery software",
                               transport=mock_precleared(calls, OK_BODY))
    assert (await initial.clear("person@example.com", state)).allowed

    updated = PreclearedClient("GB", address_source="unknown", pitch="Unrelated offer",
                               transport=mock_precleared(calls, CONSENT_BODY))
    result = await updated.clear("person@example.com", state)

    assert not result.allowed, f"Stale approval reused; upstream calls={len(calls)}"
    assert result.verdict == "consent_needed"
    assert result.receipt_id == "cc_rcpt_NO0000000001"
    assert len(calls) == 2
    # The new verdict replaced the old one, so asking again with the new facts is cached.
    assert not (await updated.clear("person@example.com", state)).allowed
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_legacy_address_only_cache_entry_is_not_reused(state):
    await state.set_setting(SETTINGS_PREFIX + "person@example.com", json.dumps({
        "verdict": "ok_to_email", "reason": "", "receipt_id": "cc_rcpt_OLD0000000001",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }))
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, CONSENT_BODY))

    result = await client.clear("person@example.com", state)

    assert not result.allowed
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_held_cold_backlog_does_not_starve_replies(state):
    pid = await seed_prospect(state)
    for step in range(24):
        await queue(state, pid, step=step + 1)
    reply_pid = await seed_prospect(state, email="reply@example.com", status="replied")
    await queue(state, reply_pid, kind="reply", to="reply@example.com")
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], status=429))

    await sender._drain_due()
    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == ["reply@example.com"]
    assert len(await state.get_outbox(status="approved")) == 24
    assert await state.get_outbox(status="failed") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], "ok_to_email", {"verdict": "maybe"}, {"reasons": []}])
async def test_malformed_response_holds_cold_email_and_reply_still_sends(state, body):
    pid = await seed_prospect(state)
    await queue(state, pid)
    reply_pid = await seed_prospect(state, email="reply@example.com", status="replied")
    await queue(state, reply_pid, kind="reply", to="reply@example.com")
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], body=body))

    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == ["reply@example.com"]
    assert await state.get_outbox(status="failed") == []
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == ["jane@acme.com"]
    # Nothing cached from a malformed answer, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + "jane@acme.com") == ""


@pytest.mark.asyncio
async def test_unexpected_optional_field_shapes_are_ignored(state):
    body = {
        "verdict": "consent_needed",
        "reasons": {"text": "not a list"},
        "next_step": 5,
        "receipt": "cc_rcpt_not_an_object",
    }
    client = PreclearedClient("GB", transport=mock_precleared([], body=body))

    result = await client.clear("person@example.com", state)

    assert not result.allowed and not result.held
    assert result.verdict == "consent_needed"
    assert result.reason == ""
    assert result.receipt_id == ""


# ── allowance messages ──


def mock_status(status, body):
    """A Precleared stand-in that answers every request with one status and body."""
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


@pytest.mark.asyncio
async def test_keyless_daily_limit_points_to_a_free_key(state):
    client = PreclearedClient("GB", transport=mock_status(429, {
        "error": "daily_limit", "get_key_url": "https://precleared.dev/get-key",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert "add PRECLEARED_API_KEY" in result.reason
    assert "precleared.dev/get-key" in result.reason


@pytest.mark.asyncio
async def test_key_out_of_checks_points_to_its_checkout_link_not_a_free_key(state):
    checkout = "https://checkout.stripe.com/c/pay/cs_test_example123#fidexample"
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_status(402, {
        "error": "payment_required",
        "message": "This key has no checks left today. Buy a pack to continue.",
        "buy_url": checkout,
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert "pack" in result.reason
    assert f"({checkout})" in result.reason
    assert "add PRECLEARED_API_KEY" not in result.reason
    # Sellbird's own wording, not the API's free text.
    assert "Buy a pack to continue" not in result.reason
    # Nothing cached, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + "person@example.com") == ""


def mock_raw(status, content):
    """A Precleared stand-in that answers every request with raw bytes."""
    return httpx.MockTransport(lambda request: httpx.Response(
        status, content=content, headers={"Content-Type": "application/json"},
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    b"",
    b"Payment required",
    b"[]",
    json.dumps({"error": "payment_required"}).encode(),
    json.dumps({"buy_url": "http://precleared.dev/buy"}).encode(),
    json.dumps({"buy_url": "javascript:alert(1)"}).encode(),
    json.dumps({"buy_url": 42}).encode(),
    json.dumps({"buy_url": "https://precleared.dev/buy\nSecond line"}).encode(),
    json.dumps({"buy_url": "https://precleared.dev/" + "a" * 2100}).encode(),
], ids=["empty", "plain-text", "json-list", "no-link", "http", "javascript",
        "not-a-string", "newline", "too-long"])
async def test_key_out_of_checks_without_a_usable_link_falls_back_to_the_buy_page(state, content):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_raw(402, content))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert "add a pack (https://precleared.dev/buy)" in result.reason
    assert "add PRECLEARED_API_KEY" not in result.reason
    assert await state.get_setting(SETTINGS_PREFIX + "person@example.com") == ""


@pytest.mark.asyncio
async def test_checkout_link_never_carries_the_api_key_into_the_reason(state):
    client = PreclearedClient("GB", api_key="pc_live_secret123", transport=mock_status(402, {
        "buy_url": "https://precleared.dev/buy?key=pc_live_secret123",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held
    assert "pc_live_secret123" not in result.reason
