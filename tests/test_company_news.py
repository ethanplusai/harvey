"""Free own-site signals: recent announcements, stated standards, named tools.

COMPANY_NEWS reads the company's own press/news/blog page (already fetched for
FUNDING_ROUND), COMPLIANCE_BADGES and TOOLS_NAMED read the homepage plus the
one trust or integrations page it links to. Each is what the site says, and an
announcement without a date is never kept or called recent.
"""

import os
import tempfile
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.collectors.profile import ProfileCollector
from harvey.company_news import (
    MAX_ITEMS, NEWS_RECENT_DAYS, kind_of, news_items, news_text, own_link,
    parse_news_text, site_facts, standards_mentioned, tools_named, TOOLS_LINK_RE,
    TRUST_LINK_RE,
)
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

TODAY = date(2026, 11, 4)
CODES = ("COMPANY_NEWS", "COMPLIANCE_BADGES", "TOOLS_NAMED")

NEWSROOM = """
<main>
 <article><h3><a href="/news/denver">Acme opens a new office in Denver</a></h3>
   <span class="date">September 3, 2026</span></article>
 <article><time datetime="2026-08-12T10:00:00Z">Aug 12</time>
   <h3>Acme appoints Pat Example as Chief Operating Officer</h3></article>
 <article><h3>Acme raises $12M Series A to grow</h3><p>2026-07-01</p></article>
 <article><h3>Acme launches claims intake portal for brokers</h3>
   <p>No date anywhere in this item, only some text that runs on.</p></article>
 <article><h3>Acme wins the Example Business Award</h3><p>March 3, 2027</p></article>
</main>
<script type="application/ld+json">{"@type":"NewsArticle",
 "headline":"Acme partners with Example Bank on payments","datePublished":"2026-06-20",
 "url":"/news/partner"}</script>
"""


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "n.db"))
        await sm.init_db()
        yield sm


# ── reading announcements ──


def test_dated_announcements_are_read_newest_first_with_their_kind():
    items = news_items(NEWSROOM, "https://acme.example/news", today=TODAY)
    assert [(i["date"].isoformat(), i["kind"]) for i in items] == [
        ("2026-09-03", "expansion"), ("2026-08-12", "leadership"), ("2026-06-20", "partnership")]
    assert items[0]["url"] == "https://acme.example/news/denver"      # the link inside the heading
    assert items[2]["url"] == "https://acme.example/news/partner"     # from structured data


def test_an_undated_item_never_takes_its_neighbours_date():
    headlines = [i["headline"] for i in news_items(NEWSROOM, "https://acme.example/news", today=TODAY)]
    assert not any("launches" in h for h in headlines)


def test_raises_are_left_to_funding_round_and_future_dates_are_ignored():
    headlines = " ".join(i["headline"] for i in news_items(NEWSROOM, "https://acme.example/news",
                                                         today=TODAY))
    assert "raises" not in headlines and "Award" not in headlines


@pytest.mark.parametrize("headline,kind", [
    ("Acme acquires Example Analytics", "acquisition"),
    ("Acme names new CFO", "leadership"),
    ("Acme welcomes Lee Example as Head of Engineering", "leadership"),
    ("Acme achieves SOC 2 Type II", "certification"),
    ("Acme teams up with Example Corp", "partnership"),
    ("Acme expands into Canada", "expansion"),
    ("Acme unveils its new client portal", "launch"),
    ("Acme named one of the best places to work", "award"),
    ("Acme raises seed round", ""),
    ("Five tips for better invoices", ""),
])
def test_kinds_of_event(headline, kind):
    assert kind_of(headline) == kind


def test_dates_in_several_formats():
    for written in ("2026-10-02", "Oct 2, 2026", "October 2nd, 2026", "2 October 2026",
                    "Oct. 2, 2026"):
        html = f"<article><h2>Acme launches a new product line</h2><p>{written}</p></article>"
        [item] = news_items(html, "https://acme.example/blog", today=TODAY)
        assert item["date"] == date(2026, 10, 2), written


def test_at_most_a_few_items_are_kept():
    html = "".join(f"<article><h2>Acme launches product number {i}</h2><p>2026-0{i}-01</p></article>"
                   for i in range(1, 9))
    assert len(news_items(html, "https://acme.example/news", today=TODAY)) == MAX_ITEMS


def test_stored_news_round_trips():
    items = news_items(NEWSROOM, "https://acme.example/news", today=TODAY)
    back = parse_news_text(news_text(items))
    assert [(b["date"], b["kind"], b["headline"]) for b in back] == [
        (i["date"], i["kind"], i["headline"]) for i in items]


# ── standards and tools ──


def test_standards_are_read_from_visible_text_only():
    html = ("<p>We are SOC 2 Type II audited, ISO/IEC 27001 certified and HIPAA ready.</p>"
            "<script>var x = 'pci fedramp';</script>")
    assert standards_mentioned(html) == ["SOC 2", "ISO 27001", "HIPAA"]


def test_tool_names_that_are_ordinary_words_need_their_capital_letter():
    html = ("<p>Syncs with QuickBooks Online, Xero and NetSuite. Payments by Stripe.</p>"
            "<p>Sage advice: a stripe of blue paint and a plaid shirt.</p>")
    assert tools_named(html) == ["QuickBooks", "Xero", "NetSuite", "Stripe"]


def test_only_the_companys_own_trust_or_integrations_page_is_followed():
    home = ('<a href="https://other.example/security">x</a>'
            '<a href="/trust">Trust center</a><a href="/integrations/">Integrations</a>')
    site = "https://acme.example"
    assert own_link(home, site, "acme.example", TRUST_LINK_RE) == "https://acme.example/trust"
    assert own_link(home, site, "acme.example", TOOLS_LINK_RE) == "https://acme.example/integrations/"
    assert own_link('<a href="https://other.example/trust">x</a>', site, "acme.example",
                    TRUST_LINK_RE) == ""


@pytest.mark.parametrize("code", CODES)
def test_each_signal_is_a_free_profile_signal(code):
    [sig] = [s for s in SIGNAL_CATALOG if s["code"] == code]
    assert sig["collector"] == "profile" and "free" in sig["cost_note"]


# ── the profile collector ──


async def _collector(state, pages, fetched):
    await seed_signal_catalog(state)
    for code in CODES + ("FUNDING_ROUND",):
        await state.set_signal_status(code, "confirmed")
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed, c.site_names, c.state, c.challenged = (
        await state.confirmed_signal_codes(), {}, state, set())

    async def resolve(self, domain):
        return "https://acme.example", pages["https://acme.example"]
    c._resolve_site = resolve.__get__(c)

    async def fetch(url):
        fetched.append(url)
        return pages.get(url, "")
    c._fetch = fetch
    return c


@pytest.mark.asyncio
async def test_the_collector_records_all_three(state):
    pages = {
        "https://acme.example": ('<p>Acme is SOC 2 audited.</p><a href="/security">Security</a>'
                                 '<a href="/integrations">Integrations</a>'),
        "https://acme.example/security": "<p>HIPAA safeguards.</p>",
        "https://acme.example/integrations": "<p>Works with QuickBooks and Salesforce.</p>",
        "https://acme.example/press": NEWSROOM + "<p>padding</p>" * 60,
    }
    fetched = []
    c = await _collector(state, pages, fetched)
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    by_code = {o["signal_code"]: o for o in await c.profile_company(cid, "acme.example")}
    news = by_code["COMPANY_NEWS"]
    assert news["value_num"] == 1 and news["value_text"].startswith("2026-")
    assert by_code["COMPLIANCE_BADGES"]["value_text"] == "SOC 2, HIPAA"
    assert by_code["TOOLS_NAMED"]["value_text"] == "QuickBooks, Salesforce"
    # The news page is the funding check's too: fetched once, not twice.
    assert fetched.count("https://acme.example/press") == 1


@pytest.mark.asyncio
async def test_finding_nothing_is_recorded_too(state):
    pages = {"https://acme.example": "<p>A plain homepage.</p>"}
    c = await _collector(state, pages, [])
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    by_code = {o["signal_code"]: o for o in await c.profile_company(cid, "acme.example")}
    for code in CODES:
        assert by_code[code]["value_num"] == 0


# ── facts for the writer ──


async def _observe(state, cid, code, num, text=""):
    await state.add_observation(company_id=cid, signal_code=code, collector="profile",
                                value_num=num, value_text=text)


@pytest.mark.asyncio
async def test_recent_news_is_a_fact_and_old_news_is_not(state):
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    recent = (TODAY - timedelta(days=30)).isoformat()
    old = (TODAY - timedelta(days=NEWS_RECENT_DAYS + 1)).isoformat()
    await _observe(state, cid, "COMPANY_NEWS", 1,
                   f"{recent} · leadership · Acme names new COO | {old} · launch · Acme launches a thing")
    await _observe(state, cid, "COMPLIANCE_BADGES", 2, "SOC 2, HIPAA")
    await _observe(state, cid, "TOOLS_NAMED", 1, "NetSuite")
    facts = await site_facts(state, cid, today=TODAY)
    text = "\n".join(facts)
    assert "Acme names new COO" in text and "about 4 weeks ago" in text
    assert "launches a thing" not in text
    assert "SOC 2, HIPAA" in text and "not proof" in text
    assert "NetSuite" in text and "doesn't say they run it" in text


@pytest.mark.asyncio
async def test_no_signals_means_no_facts(state):
    assert await site_facts(state, "") == []
    assert await site_facts(None, "c1") == []
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await _observe(state, cid, "COMPANY_NEWS", 0)
    assert await site_facts(state, cid, today=TODAY) == []


class RecordingBrain:
    def __init__(self):
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "PROMPT"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "hi", "body": "Hi"}


@pytest.mark.asyncio
async def test_the_writer_gets_the_facts(state):
    from harvey.agents.writer import Writer
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    recent = (date.today() - timedelta(days=10)).isoformat()
    await _observe(state, cid, "COMPANY_NEWS", 1, f"{recent} · acquisition · Acme acquires Example Co")
    pid = await state.add_prospect(Prospect(first_name="Ana", last_name="Doe", title="CEO",
                                            company="Acme", company_id=cid,
                                            email="ana@acme.example", email_status="verified"))
    brain = RecordingBrain()
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )
    w = Writer(brain, state, cfg)
    w.skills = ""
    await w._write_personal_email(await state.get_prospect(pid))
    assert "Acme acquires Example Co" in brain.prompts[0]
    assert "an acquisition" in brain.prompts[0]
