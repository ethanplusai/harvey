"""Startups beyond YC: other accelerators, and companies that just raised.

Same rule as the YC skill (tests/test_yc_startups.py): everything comes from
what the company says about itself on its own site, read off its homepage,
About page, press page or the search hit's own page, never from the
accelerators' directories or funding databases. ACCELERATOR records the
program (and cohort when stated); FUNDING_ROUND records the round, the
amount and the date, so the writer can tell "just raised" from "raised
three years ago".
"""

import os
import tempfile
from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio

RECENT = date.fromordinal(date.today().toordinal() - 120).strftime("%Y-%m")   # about 4 months ago

from harvey.collectors.discover import build_queries, is_junk, preset_problem
from harvey.collectors.profile import ProfileCollector, accelerators, funding_round
from harvey.models.company import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "a.db"))
        await sm.init_db()
        yield sm


def _programs(html, domain="acme.com"):
    return [(p, c) for p, c, _ in accelerators(html, domain=domain)]


# ── accelerators, as the company says it ──


@pytest.mark.parametrize("html,expected", [
    ("<p>Acme is backed by Techstars.</p>", [("Techstars", "")]),
    ("<h2>Acme (Techstars '24)</h2>", [("Techstars", "2024")]),
    ("<p>We went through Techstars Boulder 2023.</p>", [("Techstars", "2023")]),
    ("<footer>Backed by Antler</footer>", [("Antler", "")]),
    ("<p>We're an Entrepreneur First company.</p>", [("Entrepreneur First", "")]),
    ("<p>Acme is backed by South Park Commons and General Catalyst.</p>", [("South Park Commons", "")]),
    ("<p>We are backed by 500 Global.</p>", [("500 Global", "")]),
    ("<p>We were part of 500 Startups.</p>", [("500 Global", "")]),
    ("<p>We're backed by Neo.</p>", [("Neo", "")]),
    ("<p>Acme was in AI Grant batch 3.</p>", [("AI Grant", "batch 3")]),
    ("<p>We're backed by a16z speedrun (SR003).</p>", [("a16z speedrun", "SR003")]),
    ("<p>Acme went through Pioneer.</p>", [("Pioneer", "")]),
    ("<p>We are a Plug and Play portfolio company.</p>", [("Plug and Play", "")]),
    ("<p>Our investors include Antler, Techstars and angels.</p>", [("Techstars", ""), ("Antler", "")]),
    ('<img src="/b.svg" alt="Backed by Techstars">', [("Techstars", "")]),
])
def test_a_company_saying_an_accelerator_backed_it(html, expected):
    assert _programs(html) == expected


@pytest.mark.parametrize("html", [
    "<p>Trusted by 500 startups and counting.</p>",                     # a number, not 500 Startups
    "<p>We've helped 500 Startups founders and Techstars alumni grow.</p>",
    "<p>Used by Techstars-backed companies worldwide.</p>",
    "<p>We sponsored Techstars Startup Weekend Denver.</p>",
    "<p>Our founder mentors at Techstars.</p>",
    "<p>A plug-and-play platform for payments.</p>",
    "<p>We're a plug and play platform for payments.</p>",
    "<p>Our offices are in Pioneer Square, Seattle.</p>",
    "<p>We are in Pioneer Square.</p>",
    "<p>We're pioneers in clean energy.</p>",
    "<p>Acme is a pioneer in data privacy.</p>",
    "<p>We're built on Neo4j.</p>",
    "<p>We are backed by Neo Capital.</p>",
    "<p>Antlers, hides and other deer products.</p>",
    "<p>We won an AI grant from the state.</p>",
    "<p>Speedrun your onboarding with Acme.</p>",
    "<p>We're backed by a16z.</p>",                                    # a VC, not the accelerator
    "<p>Applications for Antler's next cohort close Friday.</p>",
    "<p>Our portfolio includes companies from Techstars and Antler.</p>",
    "<p>We work with accelerators like Techstars and Entrepreneur First.</p>",
    "<p>Techstars Demo Day watch party</p>",
    "<p>We're Y Combinator alumni (W19).</p>",                          # YC_BACKED's job, not this one
])
def test_mentions_that_arent_backing(html):
    assert _programs(html) == []


def test_customer_case_studies_dont_count():
    html = "<p>Foo, a Techstars company, cut costs 40% with Acme.</p>"
    assert _programs(html, domain="acme.com") == []
    assert _programs(html, domain="foo.com") == [("Techstars", "")]


# ── real phrasings, from live sites ──


def test_a_backed_by_logo_wall():
    """codos.ai: a "Backed by" label over the program's logo."""
    html = ('<div><span class="label">Backed by</span>'
            '<img class="lp-logo" src="/assets/logo/speedrun.svg" alt="a16z Speedrun"></div>')
    assert _programs(html, domain="codos.ai") == [("a16z speedrun", "")]


def test_an_investor_logo_list():
    """interhuman.ai/about: logos linked to the investors' sites."""
    html = ('<section><h2 class="sr-only">Investor and partner logos</h2><ul>'
            '<li><a href="https://psv.tech" aria-label="PSV Tech"><img alt="PSV Tech"></a></li>'
            '<li><a href="https://www.antler.co" aria-label="Antler"><img alt="Antler"></a></li></ul></section>')
    assert _programs(html, domain="interhuman.ai") == [("Antler", "")]


@pytest.mark.parametrize("html", [
    '<h2>Our customers</h2><img alt="Techstars"><img alt="Antler">',
    '<h2>Partners</h2><img alt="Plug and Play">',
    '<h2>Investors</h2>' + '<p>We build billing for teams that ship fast.</p>' * 80 + '<img alt="Techstars">',
])
def test_logos_under_other_headings_dont_count(html):
    assert _programs(html) == []


def test_founders_who_are_alumni():
    """permutive.com/about."""
    html = ("<p>Permutive was founded in 2014 by Joe Root and Tim Spratt, alumni of Entrepreneur "
            "First and Y Combinator, with a bold vision.</p>")
    assert _programs(html, domain="permutive.com") == [("Entrepreneur First", "")]


def test_a_timeline_line():
    """digitalocean.com/about."""
    html = "<li>DigitalOcean founded, joins the TechStars Accelerator Program</li>"
    assert _programs(html, domain="digitalocean.com") == [("Techstars", "")]


def test_the_yc_detector_is_unchanged():
    from harvey.collectors.profile import yc_backed
    assert yc_backed("<p>Acme is backed by Y Combinator (W22).</p>", domain="acme.com") == ("W22", 0.9)
    assert yc_backed("<p>We're backed by Techstars.</p>", domain="acme.com") is None


# ── funding rounds ──


@pytest.mark.parametrize("html,round_,amount,when", [
    ("<h1>Acme raises $12M Series A to automate billing</h1>", "Series A", "$12M", ""),
    ("<p>We raised a $4.5 million seed round led by Foo Ventures in March 2025.</p>", "Seed", "$4.5M", "2025-03"),
    ("<p>Today we're excited to announce that we've raised $20M in Series B funding.</p>", "Series B", "$20M", ""),
    ("<h2>Announcing our $3M pre-seed round</h2>", "Pre-seed", "$3M", ""),
    ("<p>Acme has closed a €8m Series A.</p>", "Series A", "€8M", ""),
    ("<p>We just raised $2,500,000 from Foo and Bar.</p>", "", "$2.5M", ""),
    ("<p>Our seed round was led by Foo Capital.</p>", "Seed", "", ""),
    ("<p>The company raised a Series C on January 14, 2026.</p>", "Series C", "", "2026-01-14"),
])
def test_a_company_announcing_its_own_raise(html, round_, amount, when):
    got = funding_round(html, domain="acme.com")
    assert got is not None
    assert (got["round"], got["amount"], got["date"]) == (round_, amount, when)


@pytest.mark.parametrize("html", [
    "<p>We help startups raise their seed round.</p>",
    "<p>Raise your Series A faster with Acme.</p>",
    "<p>Our customers have raised over $2B in Series A funding.</p>",
    "<p>We've helped founders close $500M in seed rounds.</p>",
    "<p>We have raised over $1B for our clients.</p>",
    "<p>We invested in Foo's $10M Series A.</p>",
    "<p>We led the seed round of Foo.</p>",
    "<p>We raised the bar for customer service.</p>",
    "<p>We raised $40,000 for charity last year.</p>",
    "<p>We raised a $50M fund to back seed-stage founders.</p>",
    "<p>Congrats to Foo on their Series A!</p>",
    "<p>Foo raised a $5M seed with our help.</p>",
    "<p>Acme customers raised $1B in seed funding.</p>",
    "<p>How to raise a Series A in 2026</p>",
    "<p>We raised prices in 2024.</p>",
    "<p>We raised our seed prices for spring.</p>",
    "<p>We closed $5M in ARR last year.</p>",
    "<p>We landed $2M in new contracts.</p>",
    "<p>Acme announces a $10M fund for climate startups.</p>",
    "<p>We closed our first enterprise deal.</p>",
])
def test_raising_talk_that_isnt_their_own_raise(html):
    assert funding_round(html, domain="acme.com") is None


def test_the_date_comes_from_the_page_when_the_sentence_has_none():
    html = ('<script type="application/ld+json">{"@type":"BlogPosting",'
            '"datePublished":"2026-02-10T09:00:00Z"}</script>'
            "<h1>Acme raises $9M seed</h1>")
    assert funding_round(html, domain="acme.com")["date"] == "2026-02-10"
    meta = '<meta property="article:published_time" content="2025-11-03T10:00:00+00:00"><p>We raised a $1M seed.</p>'
    assert funding_round(meta, domain="acme.com", url="https://acme.com/blog/seed")["date"] == "2025-11-03"
    timed = '<time datetime="2025-06-01">June 1</time><p>We raised our Series A.</p>'
    assert funding_round(timed, domain="acme.com", url="https://acme.com/news/series-a")["date"] == "2025-06-01"
    url = funding_round("<p>We raised our seed round.</p>", domain="acme.com",
                        url="https://acme.com/blog/2024/05/our-seed")
    assert url["date"] == "2024-05"


def test_a_listing_page_full_of_dates_gives_no_page_date():
    html = ('<time datetime="2025-06-01"></time><time datetime="2023-01-09"></time>'
            "<p>We raised our Series A.</p>")
    assert funding_round(html, domain="acme.com")["date"] == ""


def test_the_page_date_goes_to_the_raise_the_page_is_about():
    """default.com: a seed post that links to the later Series A."""
    html = ("<title>Announcing Our $6.6m Seed Round</title>"
            '<meta property="article:published_time" content="2025-07-16">'
            "<h1>Announcing Our $6.6m Seed Round</h1>"
            "<p>Today, we're launching our new product and announcing our Series A led by 8VC.</p>")
    got = funding_round(html, domain="default.com", url="https://www.default.com/post/announcing-our-seed-round")
    assert (got["round"], got["amount"], got["date"]) == ("Seed", "$6.6M", "2025-07-16")


def test_the_name_with_its_tld_is_the_company():
    """context.dev: "Context.dev has raised a $9M seed round"."""
    got = funding_round("<p>Context.dev has raised a $9M seed round, led by General Catalyst.</p>",
                        domain="context.dev")
    assert (got["round"], got["amount"]) == ("Seed", "$9M")


def test_the_newest_round_on_a_page_wins():
    html = ("<p>We raised a $2M seed round in 2021.</p>"
            "<p>In May 2024 we raised a $15M Series A.</p>"
            "<p>We raised a $15M Series A in May 2024.</p>")
    got = funding_round(html, domain="acme.com")
    assert (got["round"], got["date"]) == ("Series A", "2024-05")


def test_how_long_ago():
    from harvey.startups import funding_recency
    today = date(2026, 10, 24)
    assert funding_recency("2026-03", today) == "recent"
    assert funding_recency("2025-01-02", today) == "old"         # over 18 months
    assert funding_recency("2026", today) == "recent"
    assert funding_recency("2024", today) == "old"
    assert funding_recency("", today) == "unknown"


# ── the catalog ──


@pytest.mark.parametrize("code", ["ACCELERATOR", "FUNDING_ROUND"])
def test_the_new_signals_are_free_profile_signals(code):
    [sig] = [s for s in SIGNAL_CATALOG if s["code"] == code]
    assert sig["collector"] == "profile" and "free" in sig["cost_note"]


@pytest.mark.asyncio
async def test_they_start_proposed(state):
    await seed_signal_catalog(state)
    assert not {"ACCELERATOR", "FUNDING_ROUND"} & await state.confirmed_signal_codes()


# ── the presets ──


def _config(industries=("fintech", "dev tools")):
    return SimpleNamespace(
        icp=SimpleNamespace(industries=list(industries), geography=["United States"], geo_coordinates={}),
        product=SimpleNamespace(name="P"),
    )


def test_the_accelerator_preset_searches_every_program():
    queries = build_queries(_config(industries=["fintech"]), preset="accelerator")
    joined = " ".join(q.term for q in queries)
    for name in ("Techstars", "Antler", "Entrepreneur First", "South Park Commons", "500 Global",
                 "AI Grant", "speedrun", "Plug and Play", "Neo"):
        assert name in joined
    assert all("fintech" in q.term and q.location == "" for q in queries)
    for q in queries:   # Google reads the first 32 words of a query
        assert len(q.term.split()) <= 32
        assert "-site:linkedin.com" in q.term and "-site:crunchbase.com" in q.term


def test_the_funding_preset_searches_recent_announcements():
    [q] = build_queries(_config(industries=["fintech"]), preset="funding")
    assert '"Series A"' in q.term and "seed" in q.term and "fintech" in q.term
    assert "after:" in q.term
    for site in ("techcrunch.com", "crunchbase.com", "pitchbook.com", "linkedin.com", "prnewswire.com"):
        assert f"-site:{site}" in q.term
    assert len(q.term.split()) <= 32


@pytest.mark.parametrize("preset", ["accelerator", "funding"])
def test_the_presets_need_a_search_provider(preset):
    assert preset_problem(preset, "osm")
    assert preset_problem(preset, "serper") is None


@pytest.mark.parametrize("domain", [
    "techstars.com", "antler.co", "joinef.com", "southparkcommons.com", "500.co", "aigrant.com",
    "speedrun.a16z.com", "a16z.com", "pioneer.app", "plugandplaytechcenter.com",
    "prnewswire.com", "businesswire.com", "globenewswire.com", "finsmes.com", "axios.com",
    "venturebeat.com", "siliconangle.com", "eu-startups.com", "sifted.eu", "geekwire.com",
    "privco.com", "recentfunding.com", "pulse2.com", "startupfundraising.com",
    "vcbacked.co", "startups.gallery", "techfundingnews.com", "tech.eu",
])
def test_accelerators_and_funding_news_are_not_prospects(domain):
    assert is_junk(domain)


@pytest.mark.parametrize("domain", ["neon.tech", "acme.com", "antlerfoods.com", "pioneerplumbing.com"])
def test_lookalikes_are_still_prospects(domain):
    assert not is_junk(domain)


def test_an_accelerator_preset_hit_is_evidence():
    from harvey.collectors.discover import Business, _observations
    biz = Business(name="Acme", domain="acme.com", source_url="https://acme.com/about", provider="serper")
    q = build_queries(_config(), preset="accelerator")[0]
    [hit] = [r for r in _observations(biz, "c1", {"ACCELERATOR"}, "r1", query=q)
             if r["signal_code"] == "ACCELERATOR"]
    assert hit["value_num"] == 1 and hit["value_text"] == "" and hit["confidence"] < 0.9
    # A funding hit isn't recorded from the search: the date matters, so the page is read.
    f = build_queries(_config(), preset="funding")[0]
    assert not [r for r in _observations(biz, "c1", {"FUNDING_ROUND"}, "r1", query=f)
                if r["signal_code"] == "FUNDING_ROUND"]


def test_cli_and_dashboard_take_the_presets():
    import harvey.dashboard as dash
    from harvey.paths import PROJECT_ROOT
    for preset in ("accelerator", "funding"):
        assert dash._discovery_queries({"provider": "serper", "preset": preset}, _config())
    cli = (PROJECT_ROOT / "harvey" / "cli.py").read_text(encoding="utf-8")
    assert 'choices=["", "yc", "accelerator", "funding"]' in cli
    page = (PROJECT_ROOT / "harvey" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'value="accelerator"' in page and 'value="funding"' in page


# ── profiling ──


def _collector(state, home, pages=None, source_url=""):
    c = ProfileCollector.__new__(ProfileCollector)
    c.site_names, c.state = {}, state
    fetched = []

    async def resolve(self, domain):
        return f"https://{domain}", home
    c._resolve_site = resolve.__get__(c)

    async def get(url):
        fetched.append(url)
        return (pages or {}).get(url, "")
    c._get = get
    return c, fetched


_PAD = "<p>We build billing for teams that ship fast.</p>" * 12


@pytest.mark.asyncio
async def test_profiling_records_the_program(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("ACCELERATOR", "confirmed")
    c, _ = _collector(state, "<h1>Acme</h1><p>Acme (Techstars '24) builds billing.</p>")
    c.confirmed = await state.confirmed_signal_codes()
    cid = await state.add_company(Company(name="Acme", domain="acme.com"))
    [obs] = [o for o in await c.profile_company(cid, "acme.com") if o["signal_code"] == "ACCELERATOR"]
    assert (obs["value_num"], obs["value_text"]) == (1, "Techstars 2024")
    await state.add_observations([obs])
    assert await state.accelerator(cid) == "Techstars 2024"

    c2, _ = _collector(state, "<h1>Plain</h1><p>Family-run since 1987.</p>")
    c2.confirmed = c.confirmed
    plain = await state.add_company(Company(name="Plain", domain="plain.com"))
    [no] = [o for o in await c2.profile_company(plain, "plain.com") if o["signal_code"] == "ACCELERATOR"]
    assert no["value_num"] == 0


@pytest.mark.asyncio
async def test_profiling_reads_the_search_hit_page_for_the_raise(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("FUNDING_ROUND", "confirmed")
    cid = await state.add_company(Company(name="Acme", domain="acme.com",
                                          source_url="https://acme.com/blog/series-a"))
    post = ('<meta property="article:published_time" content="2026-05-02T08:00:00Z">'
            "<h1>Acme raises $14M Series A</h1>" + _PAD)
    c, fetched = _collector(state, "<h1>Acme</h1><p>Billing.</p>",
                            pages={"https://acme.com/blog/series-a": post})
    c.confirmed = await state.confirmed_signal_codes()
    [obs] = [o for o in await c.profile_company(cid, "acme.com") if o["signal_code"] == "FUNDING_ROUND"]
    assert obs["value_num"] == 1 and obs["value_text"] == "Series A · $14M · 2026-05-02"
    assert obs["evidence_url"] == "https://acme.com/blog/series-a"
    await state.add_observations([obs])
    assert await state.funding_round(cid) == {"round": "Series A", "amount": "$14M", "date": "2026-05-02"}


@pytest.mark.asyncio
async def test_a_hit_on_someone_elses_site_isnt_read(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("FUNDING_ROUND", "confirmed")
    cid = await state.add_company(Company(name="Acme", domain="acme.com",
                                          source_url="https://news.example.com/acme-raises"))
    c, fetched = _collector(state, "<h1>Acme</h1><p>Billing.</p>")
    c.confirmed = await state.confirmed_signal_codes()
    [obs] = [o for o in await c.profile_company(cid, "acme.com") if o["signal_code"] == "FUNDING_ROUND"]
    assert obs["value_num"] == 0
    assert not [u for u in fetched if "example.com" in u]


@pytest.mark.asyncio
async def test_the_press_page_is_checked(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("FUNDING_ROUND", "confirmed")
    cid = await state.add_company(Company(name="Acme", domain="acme.com"))
    press = "<h2>Acme raises $6M seed round (February 2026)</h2>" + _PAD
    c, _ = _collector(state, "<h1>Acme</h1><p>Billing.</p>", pages={"https://acme.com/press": press})
    c.confirmed = await state.confirmed_signal_codes()
    [obs] = [o for o in await c.profile_company(cid, "acme.com") if o["signal_code"] == "FUNDING_ROUND"]
    assert obs["value_text"] == "Seed · $6M · 2026-02"


# ── writing to them ──


class RecordingBrain:
    def __init__(self):
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "PROMPT"

    def load_skill(self, name):
        from harvey.brain import Brain
        return Brain.load_skill(self, name)

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "hi", "body": "Hi"}

    async def think(self, prompt, **kw):
        self.prompts.append(prompt)
        return "Thanks."


async def _prospect(state, code, num, text, n):
    from harvey.models.prospect import Prospect
    cid = await state.add_company(Company(name="Acme", domain=f"acme{n}.com"))
    if code:
        await state.add_observation(company_id=cid, signal_code=code, collector="profile",
                                    value_num=num, value_text=text)
    pid = await state.add_prospect(Prospect(first_name="Ana", last_name="Doe", title="CEO", company="Acme",
                                            company_id=cid, email=f"ana@acme{n}.com", email_status="verified"))
    return await state.get_prospect(pid)


def _writer(state, brain):
    from harvey.agents.writer import Writer
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )
    w = Writer(brain, state, cfg)
    w.skills = ""
    return w


@pytest.mark.asyncio
@pytest.mark.parametrize("code,num,text,playbook,fact", [
    ("ACCELERATOR", 1, "Techstars 2024", True, "Techstars 2024"),
    ("ACCELERATOR", 0, "", False, None),
    ("FUNDING_ROUND", 1, f"Series A · $12M · {RECENT}", True, "Series A, $12M"),
    ("FUNDING_ROUND", 0, "", False, None),
    ("", None, "", False, None),
])
async def test_the_writer_uses_the_playbook_for_these_startups(state, code, num, text, playbook, fact):
    await seed_signal_catalog(state)
    prospect = await _prospect(state, code, num, text, 1)
    brain = RecordingBrain()
    await _writer(state, brain)._write_personal_email(prospect)
    assert ("SELLING TO STARTUP FOUNDERS" in brain.prompts[0]) is playbook
    if fact:
        assert fact in brain.prompts[0]


@pytest.mark.asyncio
async def test_an_old_raise_is_marked_old(state):
    """For a startup known some other way (here an accelerator): an old or
    undated raise is a fact, marked so it isn't written to as news."""
    await seed_signal_catalog(state)
    for n, text, note in ((1, "Seed · $2M · 2022-03", "not recent"), (2, "Seed", "no date")):
        prospect = await _prospect(state, "ACCELERATOR", 1, "Antler", n)
        await state.add_observation(company_id=prospect.company_id, signal_code="FUNDING_ROUND",
                                    collector="profile", value_num=1, value_text=text)
        brain = RecordingBrain()
        await _writer(state, brain)._write_personal_email(prospect)
        assert note in brain.prompts[0] and "SELLING TO STARTUP FOUNDERS" in brain.prompts[0]


@pytest.mark.asyncio
async def test_the_reply_handler_uses_it_for_an_accelerator_startup(state):
    from harvey.agents.handler import Handler
    from harvey.models.conversation import Conversation, Message
    from tests.test_outbox_native import Cfg, Env
    await seed_signal_catalog(state)
    prospect = await _prospect(state, "ACCELERATOR", 1, "Antler", 1)
    brain = RecordingBrain()
    handler = Handler(brain=brain, state=state, config=Cfg(), env=Env())
    convo = Conversation(id="c", prospect_id=prospect.id,
                         thread=[Message(sender="prospect", content="What does it cost?")])
    await handler._generate_response("question", "What does it cost?", prospect, convo)
    assert "SELLING TO STARTUP FOUNDERS" in brain.prompts[0]


@pytest.mark.asyncio
async def test_a_mixed_sequence_batch_gets_no_founder_framing(state):
    await seed_signal_catalog(state)
    funded = await _prospect(state, "FUNDING_ROUND", 1, f"Seed · {RECENT}", 1)
    plain = await _prospect(state, "", None, "", 2)
    brain = RecordingBrain()
    await _writer(state, brain)._write_sequence([funded, plain])
    assert "SELLING TO STARTUP FOUNDERS" not in brain.prompts[0]
    brain = RecordingBrain()
    await _writer(state, brain)._write_sequence([funded])
    assert "SELLING TO STARTUP FOUNDERS" in brain.prompts[0]


def test_the_playbook_covers_accelerators_and_raises():
    from harvey.paths import PROJECT_ROOT
    text = (PROJECT_ROOT / "skills" / "selling_to_startups.md").read_text(encoding="utf-8").lower()
    for topic in ("accelerator", "techstars", "recently funded", "18 months", "amount"):
        assert topic in text


# ── review of #78: local businesses, investors, dates, ordinary words ──


@pytest.mark.parametrize("html", [
    "<p>We've closed over $500M in loans for homebuyers.</p>",          # mortgage broker
    "<p>We've closed over $500M in loans.</p>",
    "<p>We closed $100M in transactions last year.</p>",                 # realtor
    "<p>We have secured over $100 million in verdicts and settlements.</p>",   # law firm
    "<p>We secured $2.4M for injured clients.</p>",
    "<p>We raised $1M for St. Jude Children's Research Hospital.</p>",
    "<p>We've completed over 1,000 projects worth $500M.</p>",           # contractor
    "<p>We secured a $1.5M loan.</p>",
    "<p>We raised $1.2M at our annual gala.</p>",
    "<p>We announced our Series A investment in Acme Robotics.</p>",     # an investor's site
    "<p>We closed a $5M seed round for Acme Robotics.</p>",
    "<p>We led the $5M seed round in Acme Robotics.</p>",
    "<p>We announced our Series A-ready program for founders.</p>",
    "<p>We raised our prices by $10.</p>",
    "<p>We have never raised a seed round.</p>",
    "<p>We raised the bar for 2026.</p>",
])
def test_money_that_isnt_a_raise(html):
    assert funding_round(html, domain="acme.com") is None


@pytest.mark.parametrize("html,round_,amount,when", [
    ("<p>We raised $5M Series A in 2021, and in March 2025 we launched Acme Cloud.</p>", "Series A", "$5M", "2021"),
    ("<p>We raised €1,2M in our seed round.</p>", "Seed", "€1.2M", ""),
    ("<p>We raised £3.4m Series A.</p>", "Series A", "£3.4M", ""),
    ("<p>We raised a 1,2 Mio Euro seed round.</p>", "Seed", "€1.2M", ""),
    ("<p>We raised a Series B extension of $20M.</p>", "Series B", "$20M", ""),
    ("<p>We are hiring! We raised $20M Series B in 2025.</p>", "Series B", "$20M", "2025"),
    ("<p>We raised $12M to expand in 2099.</p>", "", "$12M", ""),           # a future year isn't the raise's date
    ("<h1>Teton.ai Raises $20 Million to Reinvent Elderly Care</h1>", "", "$20M", ""),   # live: teton.ai
])
def test_raise_details_from_the_right_clause(html, round_, amount, when):
    got = funding_round(html, domain="teton.ai" if "Teton" in html else "acme.com")
    assert got is not None and (got["round"], got["amount"], got["date"]) == (round_, amount, when)


@pytest.mark.parametrize("html,url", [
    ('<script type="application/ld+json">{"@type":"WebPage","datePublished":"2026-06-01T00:00:00+00:00"}</script>'
     "<p>We raised a $10M Series A led by Sequoia.</p>", "https://acme.com/about"),        # Yoast on About
    ('<p>We raised a $10M Series A led by Sequoia.</p><div>Latest post <time datetime="2026-10-01">Oct 1</time></div>',
     "https://acme.com/"),
    ('<meta name="date" content="2026-10-20"><p>We raised a $8M seed round led by X.</p>', "https://acme.com/careers"),
])
def test_page_dates_only_on_article_pages(html, url):
    assert funding_round(html, domain="acme.com", url=url)["date"] == ""


def test_an_article_type_in_structured_data_is_an_article():
    html = ('<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-06-01"}</script>'
            "<p>We raised a $10M Series A led by Sequoia.</p>")
    assert funding_round(html, domain="acme.com", url="https://acme.com/acme-series-a")["date"] == "2026-06-01"
    assert funding_round("<p>We raised a $8M seed round.</p>", domain="acme.com",
                         url="https://acme.com/blog/2026/10/hello")["date"] == "2026-10"


@pytest.mark.parametrize("html", [
    "<p>We were part of Techstars Startup Weekend 2019.</p>",
    "<p>We participated in Techstars Startup Weekend Boulder 2022.</p>",
    "<p>We joined Techstars Demo Day 2024 as a sponsor.</p>",
    "<p>We are an entrepreneur first company.</p>",
    "<p>We're part of 500 Global retailers' supply chains.</p>",
    "<p>We are backed by 500 global brands.</p>",
    "<p>We are proud members of the Pioneer dealer network.</p>",
    "<p>We're part of Antler Homes, a family builder.</p>",
    "<p>We are a Plug and Play solution for clinics.</p>",
    "<p>We are the Pioneer of AI scheduling.</p>",
    "<p>We are part of the Pioneer Valley business community.</p>",
    "<p>We are backed by Pioneer Square Labs.</p>",
    "<p>We joined the Neo Scholars program.</p>",
    '<p>Investors</p><div>Customers</div><img alt="Techstars">',
])
def test_reviewed_accelerator_lookalikes(html):
    assert _programs(html) == []


@pytest.mark.parametrize("html,expected", [
    ("<p>We are an Entrepreneur First company.</p>", [("Entrepreneur First", "")]),
    ("<p>We joined Plug and Play Silicon Valley 2023.</p>", [("Plug and Play", "2023")]),
    ("<p>We're backed by Neo and Sequoia.</p>", [("Neo", "")]),
])
def test_reviewed_accelerator_claims_still_count(html, expected):
    assert _programs(html) == expected


def test_the_junk_list_has_neo_and_the_search_skips_pioneer():
    assert is_junk("neo.com")
    joined = " ".join(q.term for q in build_queries(_config(industries=["fintech"]), preset="accelerator"))
    assert "Pioneer" not in joined         # "backed by Pioneer" finds Pioneer Square Labs and Pioneer Fund


@pytest.mark.asyncio
@pytest.mark.parametrize("text,playbook", [
    (f"Series A · $12M · {RECENT}", True),
    ("Series A · $12M · 2013-02", False),       # old: a fact, not a reason for founder framing
    ("$100M", False),                          # undated
])
async def test_only_a_recent_raise_brings_the_playbook(state, text, playbook):
    await seed_signal_catalog(state)
    prospect = await _prospect(state, "FUNDING_ROUND", 1, text, 1)
    brain = RecordingBrain()
    await _writer(state, brain)._write_personal_email(prospect)
    assert ("SELLING TO STARTUP FOUNDERS" in brain.prompts[0]) is playbook


@pytest.mark.asyncio
async def test_a_profile_that_finds_nothing_supersedes_an_accelerator_search_hit(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("ACCELERATOR", "confirmed")
    cid = await state.add_company(Company(name="Acme", domain="acme.com"))
    await state.add_observation(company_id=cid, signal_code="ACCELERATOR", collector="discover",
                                value_num=1, confidence=0.6, observed_at="2026-01-01T00:00:00")
    c, _ = _collector(state, "<h1>Acme</h1><p>Billing.</p>")
    c.confirmed = await state.confirmed_signal_codes()
    [no] = [o for o in await c.profile_company(cid, "acme.com") if o["signal_code"] == "ACCELERATOR"]
    assert no["value_num"] == 0
    await state.add_observations([no])
    assert await state.accelerator(cid) is None
