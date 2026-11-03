"""Warmup: the cold-email ramp for a new domain, and `sellbird warmup`'s
read-only deliverability checks.

Based on community PR #18 by Tariz Shahid.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dns.exception
import pytest

from sellbird import deliverability as d
from sellbird.config import EmailChannelConfig, WarmupConfig
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_prospect, state,
)


# ── The ramp ──

def test_no_start_date_means_no_ramp():
    assert WarmupConfig().daily_cap(30) == 30
    assert EmailChannelConfig(max_daily_sends=12).cold_daily_cap() == 12


def test_ramp_holds_then_climbs_to_ceiling():
    w = WarmupConfig(start_date="2026-10-01", min_days=14, start_sends=5, daily_increase=3)
    cap = lambda day: w.daily_cap(30, date(2026, 10, day))  # noqa: E731
    assert [cap(1), cap(14)] == [0, 0]          # warming only
    assert [cap(15), cap(16), cap(20)] == [5, 8, 20]
    assert cap(31) == 30                         # never above max_daily_sends


def test_bad_or_blank_settings():
    with pytest.raises(ValueError):
        WarmupConfig(start_date="10/01/2026")
    with pytest.raises(ValueError):
        WarmupConfig(min_days=-1)
    assert WarmupConfig(start_date=None).start_date == ""  # left blank in YAML


def _now_iso(**delta):
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(**delta)).isoformat()


async def _approved(state, pid, kind, step=1, campaign="c1"):
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello there", body="A short, plain note. Question?", send_at=_now_iso(),
        status="approved", campaign_id=campaign if kind == "sequence" else "", step=step, kind=kind)


def _ramped_sender(state, provider, monkeypatch, days_in, **ramp):
    sender = make_sender(state, provider)
    start = (date.today() - timedelta(days=days_in)).isoformat()
    # The test config is a shared class: monkeypatch restores it afterwards.
    monkeypatch.setattr(sender.config.channels.email, "warmup",
                        WarmupConfig(start_date=start, **ramp), raising=False)
    return sender


@pytest.mark.asyncio
async def test_while_warming_cold_email_waits_but_replies_go(state, monkeypatch):
    cold = await seed_prospect(state, email="jane@acme.com", status="queued")
    await _approved(state, cold, "sequence")
    warm = await seed_prospect(state, email="bob@acme.com", status="replied")
    await state.add_outbox_item(
        prospect_id=warm, to_email="bob@acme.com", subject="Re: hi",
        body="Sure, Tuesday works.", send_at=_now_iso(), status="approved", kind="reply")
    provider = FakeProvider()
    await _ramped_sender(state, provider, monkeypatch, days_in=3, min_days=14)._drain_due()
    assert [m["to"] for m in provider.sent] == ["bob@acme.com"]
    assert len(await state.get_outbox(status="approved")) == 1  # the cold one waits


@pytest.mark.asyncio
async def test_after_warming_cold_email_is_capped_at_todays_ramp(state, monkeypatch):
    for n in range(4):
        pid = await seed_prospect(state, email=f"p{n}@acme.com", status="queued")
        await state.add_outbox_item(
            prospect_id=pid, to_email=f"p{n}@acme.com", subject="hello there",
            body="A short, plain note. Question?", send_at=_now_iso(), status="approved",
            campaign_id="c1", step=1)
    provider = FakeProvider()
    # Day 15 of a 14-day warmup: start_sends + one day's increase = 2.
    await _ramped_sender(state, provider, monkeypatch, days_in=15, min_days=14,
                         start_sends=1, daily_increase=1)._drain_due()
    assert len(provider.sent) == 2


@pytest.mark.asyncio
async def test_the_ramp_leaves_instantly_alone(state, monkeypatch):
    # Instantly runs its own warmup and per-account limits; capping uploads
    # here would strand the rest of a campaign's prospects.
    from sellbird.agents.sender import Sender
    from sellbird.config import EnvConfig
    from sellbird.models.campaign import Campaign, EmailStep
    ids = [await seed_prospect(state, email=f"p{n}@acme.com", status="queued") for n in range(6)]
    warm_start = (date.today() - timedelta(days=14)).isoformat()
    config = SimpleNamespace(channels=SimpleNamespace(email=EmailChannelConfig(
        provider="instantly", require_approval=False, max_daily_sends=50,
        warmup=WarmupConfig(start_date=warm_start, min_days=14, start_sends=3))))
    sender = Sender(None, state, config, EnvConfig())
    sender.instantly = AsyncMock()
    sender.instantly.set_campaign_emails.return_value = {}
    sender.instantly.add_leads.return_value = {}
    await sender._deploy_campaign(Campaign(
        id="c", name="Draft", instantly_campaign_id="existing", prospect_ids=ids,
        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")]))
    leads = sender.instantly.add_leads.await_args.args[1]
    assert len(leads) == 6


# ── The checks (DNS stubbed) ──

def _txt(records):
    return lambda name: records.get(name, [])


def test_spf_checks(monkeypatch):
    monkeypatch.setattr(d, "_txt", _txt({"a.com": ["v=spf1 include:_spf.google.com ~all"]}))
    assert d.check_spf("a.com").status == d.OK
    monkeypatch.setattr(d, "_txt", _txt({"a.com": ["v=spf1 +all"]}))
    assert d.check_spf("a.com").status == d.FAIL
    monkeypatch.setattr(d, "_txt", _txt({"a.com": ["v=spf1 -all", "v=spf1 ~all"]}))
    assert d.check_spf("a.com").status == d.FAIL
    monkeypatch.setattr(d, "_txt", _txt({}))
    assert d.check_spf("a.com").status == d.FAIL


def test_dmarc_and_dkim(monkeypatch):
    monkeypatch.setattr(d, "_txt", _txt({
        "_dmarc.a.com": ["v=DMARC1; p=none; rua=mailto:x@a.com"],
        "google._domainkey.a.com": ["v=DKIM1; k=rsa; p=MIIBIjANBg"],
        "s1._domainkey.b.com": ["v=DKIM1; p="],  # revoked key
    }))
    assert d.check_dmarc("a.com").status == d.WARN
    assert d.check_dmarc("b.com").status == d.FAIL
    assert d.check_dkim("a.com").status == d.OK
    assert d.check_dkim("b.com", "s1").status == d.FAIL


def test_blocklist_refusal_is_not_a_listing(monkeypatch):
    answers = {"a.com.dbl.spamhaus.org": ["127.255.255.254"],
               "a.com.multi.surbl.org": ["127.0.0.2"]}
    monkeypatch.setattr(d, "_a", lambda name: answers.get(name, []))
    by_zone = {c.name.split()[-1]: c.status for c in d.check_domain_blocklists("a.com")}
    assert by_zone == {"dbl.spamhaus.org": d.INFO, "multi.surbl.org": d.FAIL,
                       "multi.uribl.com": d.OK}


def test_a_dns_timeout_is_unknown_not_missing(monkeypatch):
    def timeout(*a, **k):
        raise dns.exception.Timeout()
    monkeypatch.setattr(d.dns.resolver, "resolve", timeout)
    checks = [d.check_mx("a.com"), d.check_spf("a.com"), d.check_dkim("a.com"),
              d.check_dmarc("a.com"), *d.check_domain_blocklists("a.com"),
              *d.check_ip_blocklists("smtp.a.com")]
    assert {c.status for c in checks} == {d.INFO}


def test_warmup_check_reports_progress():
    w = WarmupConfig(start_date="2026-10-01", min_days=14)
    assert d.check_warmup(w, 30, date(2026, 10, 5)).status == d.FAIL
    assert d.check_warmup(w, 30, date(2026, 10, 20)).status == d.OK
    assert d.check_warmup(WarmupConfig(), 30).status == d.WARN


@pytest.mark.parametrize("domain, ok", [
    ("acme.com", True), ("mail.acme.co.uk", True),
    ("acme", False), ("acme.com/../x", False), ("-acme.com", False), ("", False),
])
def test_only_real_domain_names_are_checked(domain, ok):
    assert bool(d.DOMAIN_RE.match(domain)) is ok


def test_an_unquoted_yaml_date_and_blank_values_load():
    import yaml
    email = EmailChannelConfig(**yaml.safe_load(
        "warmup:\n  start_date: 2026-10-01\n  min_days:\n  dkim_selector:\n"))
    assert email.warmup.start_date == "2026-10-01" and email.warmup.min_days == 14
    assert EmailChannelConfig(**yaml.safe_load("warmup:\n")).warmup == WarmupConfig()


def test_failed_lookups_are_flagged_unknown(monkeypatch):
    def timeout(*a, **k):
        raise dns.exception.Timeout()
    monkeypatch.setattr(d.dns.resolver, "resolve", timeout)
    assert d.check_spf("a.com").unknown and not d.check_warmup(WarmupConfig(), 30).unknown


def test_spf_without_all_and_duplicate_dmarc(monkeypatch):
    monkeypatch.setattr(d, "_txt", _txt({
        "a.com": ["v=spf1 include:_spf.google.com"],
        "_dmarc.a.com": ["v=DMARC1; p=none", "v=DMARC1; p=reject"],
        "_dmarc.b.com": ["v=DMARC1; rua=mailto:x@b.com"],
        "c.com": ["v=spf1 a/24 mx/24 " + " ".join(f"include:i{n}.com" for n in range(9)) + " -all"],
    }))
    assert d.check_spf("a.com").status == d.WARN
    assert d.check_dmarc("a.com").status == d.FAIL
    assert d.check_dmarc("b.com").status == d.WARN
    assert d.check_spf("c.com").status == d.FAIL  # a/24 and mx/24 are lookups too


def test_a_ramp_that_never_grows_says_so():
    w = WarmupConfig(start_date="2026-10-01", min_days=0, start_sends=5, daily_increase=0)
    assert "stays at 5/day" in d.check_warmup(w, 30, date(2026, 10, 5)).detail
    w = WarmupConfig(start_date="2026-10-01", min_days=0, start_sends=0, daily_increase=0)
    assert d.check_warmup(w, 30, date(2026, 10, 5)).status == d.WARN


@pytest.mark.parametrize("domain", ["xn--mnchen-3ya.de", "shop.xn--p1ai"])
def test_punycode_domains_are_checked(domain):
    assert d.DOMAIN_RE.match(domain)


def test_timestamp_start_date_and_instantly_note():
    import yaml
    email = EmailChannelConfig(**yaml.safe_load("warmup:\n  start_date: 2026-10-01 09:00\n"))
    assert email.warmup.start_date == "2026-10-01"
    w = WarmupConfig(start_date="2026-10-01", min_days=0, start_sends=0, daily_increase=2)
    assert d.check_warmup(w, 30, date(2026, 10, 1)).status != d.WARN  # 0 today, grows
    assert d.check_warmup(w, 30, provider="instantly").status == d.INFO
