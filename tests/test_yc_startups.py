"""Harvey's built-in skill for YC startups: find them, tag them, write to them.

Without touching ycombinator.com, whose terms forbid scraping and
harvesting contact details. A company says it's YC-backed on its own site
(YC_BACKED, read with the other free homepage signals); the "yc" discovery
preset finds such sites through your own search provider; and the
selling_to_startups skill shapes what the writer and reply handler say to
those founders.
"""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.collectors.discover import build_queries, is_junk, preset_problem
from harvey.collectors.profile import ProfileCollector, yc_backed
from harvey.models.company import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "y.db"))
        await sm.init_db()
        yield sm


# ── reading it off their own site ──


@pytest.mark.parametrize("html,batch", [
    ("<p>Acme is backed by Y Combinator.</p>", ""),
    ("<div>YC-backed</div><p>Analytics for teams</p>", ""),
    ("<h2>Acme (YC W24)</h2>", "W24"),
    ("<p>Proud to be part of Y Combinator's Summer 2023 batch</p>", "S23"),
    ("<footer>Y Combinator S21</footer>", "S21"),
    ("<p>We're a YC X25 company</p>", "X25"),
    ("<p>Backed by YC (F24)</p>", "F24"),
    ('<a href="https://www.ycombinator.com/companies/acme">Our YC profile</a>', ""),
    ('<img src="/yc.svg" alt="Backed by Y Combinator">', ""),
    ("<p>We're Y Combinator alumni (W19).</p>", "W19"),
])
def test_a_company_saying_its_yc_backed(html, batch):
    got = yc_backed(html, domain="acme.com")
    assert got is not None
    assert got[0] == batch and 0 < got[1] <= 1


@pytest.mark.parametrize("html", [
    '<a href="https://news.ycombinator.com/item?id=1">Discuss on Hacker News</a>',
    "<p>We read Paul Graham's essays on Y Combinator's blog.</p>",
    "<p>Tips we learned from Y Combinator's Startup School</p>",
    "<p>Our competitor was in YC; we bootstrapped.</p>",
    "<p>Acme for YC founders and everyone else</p>",
    "<p>We sell to Y Combinator companies.</p>",
])
def test_mentioning_yc_isnt_being_backed_by_it(html):
    assert yc_backed(html) is None


def test_the_signal_is_in_the_catalog_and_starts_proposed():
    [sig] = [s for s in SIGNAL_CATALOG if s["code"] == "YC_BACKED"]
    assert sig["collector"] == "profile" and sig["value_type"] == "bool"
    assert "free" in sig["cost_note"]


@pytest.mark.asyncio
async def test_the_profile_collector_records_it(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("YC_BACKED", "confirmed")
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed, c.site_names, c.state = await state.confirmed_signal_codes(), {}, state

    pages = {"acme.com": "<h1>Acme</h1><p>Acme (YC W24) builds billing.</p>",
             "plain.com": "<h1>Plain</h1><p>Family-run since 1987.</p>"}

    async def resolve(self, domain):
        return f"https://{domain}", pages[domain]
    c._resolve_site = resolve.__get__(c)

    async def get(url):
        return ""
    c._get = get
    acme = await state.add_company(Company(name="Acme", domain="acme.com"))
    plain = await state.add_company(Company(name="Plain", domain="plain.com"))
    [yes] = [o for o in await c.profile_company(acme, "acme.com") if o["signal_code"] == "YC_BACKED"]
    [no] = [o for o in await c.profile_company(plain, "plain.com") if o["signal_code"] == "YC_BACKED"]
    assert (yes["value_num"], yes["value_text"]) == (1, "W24")
    assert no["value_num"] == 0            # checked, not YC: a finding too


# ── finding them ──


def _config(industries=("fintech", "dev tools"), geography=("United States",)):
    return SimpleNamespace(
        icp=SimpleNamespace(industries=list(industries), geography=list(geography), geo_coordinates={}),
        product=SimpleNamespace(name="P"),
    )


def test_the_yc_preset_searches_for_companies_that_say_so():
    queries = build_queries(_config(), preset="yc")
    terms = [q.term for q in queries]
    assert len(queries) == 2 and all(q.location == "" for q in queries)   # startups aren't local
    assert all('"backed by Y Combinator"' in t for t in terms)
    assert "fintech" in terms[0] and "dev tools" in terms[1]
    assert all("-site:ycombinator.com" in t for t in terms)            # never their site
    # Cities only when asked for.
    assert {q.location for q in build_queries(_config(), cities=["Austin, TX"], preset="yc")} == {"Austin, TX"}


def test_the_yc_preset_needs_a_search_provider():
    assert preset_problem("yc", "osm") and "search" in preset_problem("yc", "osm")
    assert preset_problem("yc", "dataforseo_listings")
    assert preset_problem("yc", "serper") is None
    assert preset_problem("yc", "tinyfish") is None
    assert preset_problem("yc", "dataforseo_serp") is None
    assert preset_problem("", "osm") is None
    assert preset_problem("nope", "serper")


@pytest.mark.parametrize("domain", [
    "ycombinator.com", "news.ycombinator.com", "crunchbase.com", "techcrunch.com",
    "wellfound.com", "angel.co", "tracxn.com", "pitchbook.com", "cbinsights.com",
    "producthunt.com", "github.com", "f6s.com",
])
def test_startup_databases_and_press_are_not_prospects(domain):
    assert is_junk(domain)


def test_cli_estimate_with_the_preset(tmp_path, monkeypatch, capsys):
    import argparse
    import asyncio
    from harvey import cli
    import harvey.config as config_mod
    import harvey.state as state_mod
    real = state_mod.StateManager
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **k: real(str(tmp_path / "c.db")))
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: _config())
    monkeypatch.setattr(config_mod, "load_env",
                       lambda: SimpleNamespace(model_dump=lambda: {"serper_api_key": "x"}))
    args = argparse.Namespace(providers=False, provider="serper", city="", depth=30, limit=100,
                              max_spend=1.0, estimate=True, preset="yc")
    cli.cmd_discover(args)
    out = capsys.readouterr().out
    assert "backed by Y Combinator" in out
    args.provider = "osm"
    with pytest.raises(SystemExit):
        cli.cmd_discover(args)


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
        return "Thanks, here's more."


def test_the_skill_ships_with_harvey():
    from harvey.paths import PROJECT_ROOT
    text = (PROJECT_ROOT / "skills" / "selling_to_startups.md").read_text(encoding="utf-8")
    assert len(text.split()) > 300
    for topic in ("founder", "batch", "Demo Day"):
        assert topic.lower() in text.lower()


async def _yc_company(state, backed: bool):
    from harvey.models.prospect import Prospect
    cid = await state.add_company(Company(name="Acme", domain=f"acme{int(backed)}.com"))
    await state.add_observation(company_id=cid, signal_code="YC_BACKED", collector="profile",
                                value_num=1 if backed else 0, value_text="W24" if backed else "")
    pid = await state.add_prospect(Prospect(first_name="Ana", last_name="Doe", title="CEO", company="Acme",
                                            company_id=cid, email=f"ana@acme{int(backed)}.com",
                                            email_status="verified"))
    return await state.get_prospect(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("backed", [True, False])
async def test_the_writer_uses_the_skill_for_yc_startups_only(state, backed):
    from harvey.agents.writer import Writer
    await seed_signal_catalog(state)
    prospect = await _yc_company(state, backed)
    brain = RecordingBrain()
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )
    w = Writer(brain, state, cfg)
    w.skills = ""
    await w._write_personal_email(prospect)
    await w._write_sequence([prospect])
    for prompt in brain.prompts:
        assert ("SELLING TO STARTUP FOUNDERS" in prompt) is backed
    if backed:
        assert "YC W24" in brain.prompts[0]               # the fact itself, for the email


@pytest.mark.asyncio
@pytest.mark.parametrize("backed", [True, False])
async def test_the_reply_handler_uses_it_too(state, backed):
    from harvey.agents.handler import Handler
    from harvey.models.conversation import Conversation, Message
    from tests.test_outbox_native import Cfg, Env
    await seed_signal_catalog(state)
    prospect = await _yc_company(state, backed)
    brain = RecordingBrain()
    handler = Handler(brain=brain, state=state, config=Cfg(), env=Env())
    convo = Conversation(id="c", prospect_id=prospect.id,
                         thread=[Message(sender="prospect", content="What does it cost?")])
    await handler._generate_response("question", "What does it cost?", prospect, convo)
    assert ("SELLING TO STARTUP FOUNDERS" in brain.prompts[0]) is backed


def test_the_dashboard_estimate_takes_the_preset(monkeypatch):
    import harvey.dashboard as dash
    from tests.test_dashboard_signals import client as _client  # noqa: F401
    import harvey.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: _config())
    queries = dash._discovery_queries({"provider": "serper", "preset": "yc"}, _config())
    assert all('"backed by Y Combinator"' in q.term for q in queries)
    with pytest.raises(ValueError):
        dash._discovery_queries({"provider": "osm", "preset": "yc"}, _config())


# ── what real YC company sites look like (checked against live homepages) ──


def test_a_yc_page_in_their_structured_data_counts():
    """Resend and Airbyte list their YC page in JSON-LD sameAs, not as a link."""
    html = ('<script type="application/ld+json">{"@type":"Organization","sameAs":'
            '["https://github.com/resend","https://www.ycombinator.com/companies/resend"]}</script>')
    assert yc_backed(html, domain="resend.com") == ("", 0.95)


def test_someone_elses_yc_page_doesnt_make_them_yc():
    """A customer logo linking to that customer's YC page isn't backing."""
    html = '<a href="https://www.ycombinator.com/companies/acme">Acme, our customer</a>'
    assert yc_backed(html, domain="billingtool.com") is None
    assert yc_backed(html, domain="acme.io") == ("", 0.95)
    assert yc_backed(html, domain="getacme.com") == ("", 0.95)        # slug inside the name


@pytest.mark.parametrize("html", [
    '<span>Powering a quarter of the last YC batch to 40% of the Forbes AI 50.</span>',  # Mintlify
    '<a href="/customers/ycombinator">Y Combinator</a>',                                  # PostHog
    '<a href="/company/ycombinator-startup-promotion">YC deal</a>',                       # Airbyte
])
def test_real_ways_sites_mention_yc_without_being_backed(html):
    assert yc_backed(html, domain="example.com") is None


@pytest.mark.asyncio
async def test_the_about_page_is_checked_when_the_homepage_is_silent(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("YC_BACKED", "confirmed")
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed, c.site_names, c.state = await state.confirmed_signal_codes(), {}, state
    fetched = []

    async def resolve(self, domain):
        return f"https://{domain}", "<h1>Acme</h1><p>Open-source billing.</p>"
    c._resolve_site = resolve.__get__(c)

    async def get(url):
        fetched.append(url)
        if url.endswith("/about"):   # a real page (the collector skips stubs under 500 bytes)
            return ("<h2>Our story</h2><p>We went through Y Combinator in Winter 2021.</p>"
                    + "<p>We build billing for teams that ship fast.</p>" * 12)
        return ""
    c._get = get
    cid = await state.add_company(Company(name="Acme", domain="acme.com"))
    [yes] = [o for o in await c.profile_company(cid, "acme.com") if o["signal_code"] == "YC_BACKED"]
    assert (yes["value_num"], yes["value_text"]) == (1, "W21")
    assert yes["evidence_url"] == "https://acme.com/about"


def test_an_encoded_apostrophe_isnt_a_batch():
    """posthog.com/about: "Y Combinator&#x27;s W20 batch" read as X27."""
    html = "<p>PostHog was hatched in Y Combinator&#x27;s W20 batch.</p><p>Y Combinator&#x27;s best</p>"
    assert yc_backed(html, domain="posthog.com") == ("W20", 0.9)


# ── the preset's hit is evidence, and profiling doesn't erase it ──


def test_a_preset_hit_records_that_the_search_matched():
    from harvey.collectors.discover import Business, _observations
    biz = Business(name="Acme", domain="acme.com", source_url="https://acme.com/blog/raise", provider="serper")
    yc = build_queries(_config(), preset="yc")[0]
    plain = build_queries(_config())[0]
    rows = _observations(biz, "c1", {"YC_BACKED", "FOUND_IN_SERP"}, "r1", query=yc)
    [hit] = [r for r in rows if r["signal_code"] == "YC_BACKED"]
    assert hit["value_num"] == 1 and hit["confidence"] < 0.9
    assert hit["evidence_url"] == "https://acme.com/blog/raise"
    assert not [r for r in _observations(biz, "c1", {"YC_BACKED"}, "r1", query=plain)
                if r["signal_code"] == "YC_BACKED"]
    assert not _observations(biz, "c1", set(), "r1", query=yc)            # unconfirmed: nothing


@pytest.mark.asyncio
async def test_profiling_doesnt_overwrite_yc_evidence_with_not_stated(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("YC_BACKED", "confirmed")
    cid = await state.add_company(Company(name="Acme", domain="acme.com"))
    await state.add_observation(company_id=cid, signal_code="YC_BACKED", collector="discover",
                                value_num=1, confidence=0.7, evidence_url="https://acme.com/blog/raise")
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed, c.site_names, c.state = await state.confirmed_signal_codes(), {}, state

    async def resolve(self, domain):
        return f"https://{domain}", "<h1>Acme</h1><p>Billing.</p>"
    c._resolve_site = resolve.__get__(c)

    async def get(url):
        return ""
    c._get = get
    obs = await c.profile_company(cid, "acme.com")
    assert not [o for o in obs if o["signal_code"] == "YC_BACKED"]   # silent pages aren't a "no"
    assert await state.yc_batch(cid) == ""


# ── review: wrongly tagged, and tags that couldn't be undone ──


@pytest.mark.parametrize("html", [
    "<p>Trusted by 300 YC-backed startups and the Fortune 500.</p>",
    "<p>Used by YC alumni at Stripe, Ramp and Brex.</p>",
    "<p>Built for a YC company like yours.</p>",
    "<p>We're part of the YC Startup School community.</p>",
    "<p>We went through YC Startup School in 2022.</p>",
    "<p>We cover every YC company in the W24 batch.</p>",
    "<p>Our portfolio includes companies from Y Combinator S23.</p>",
    "<p>Exclusive deal for YC companies. Apply for YC S25 with our help.</p>",
    "<p>YC Demo Day watch party - Fall 2024</p>",
    "<p>YC-approved phone: Galaxy S24 Ultra</p>",
    "<p>YC founders using our plan W20 get 50% off.</p>",
])
def test_customer_and_marketing_lines_arent_backing(html):
    assert yc_backed(html, domain="vendor.com") is None


@pytest.mark.parametrize("html,batch", [
    ("<p>Our investors include Y Combinator, SV Angel and others.</p>", ""),
    ("<p>Supported by Y Combinator</p>", ""),
    ("<p>Investors: Y Combinator</p>", ""),
    ("<p>We were in the Summer 2023 batch of Y Combinator.</p>", "S23"),
    ("<p>Acme is a Y Combinator company (W22).</p>", "W22"),
])
def test_more_real_ways_to_say_it(html, batch):
    got = yc_backed(html, domain="acme.com")
    assert got is not None and got[0] == batch


@pytest.mark.parametrize("slug,domain,ours", [
    ("stripe", "", False),               # no domain to compare: not theirs
    ("air", "airtable.com", False),      # too short to be a match
    ("acme", "app.acme.com", True),      # the registrable name, not "app"
    ("acme", "acme.co.uk", True),
    ("acme", "getacme.com", True),
    ("acmehq", "acme.com", True),
    ("airtable", "airbnb.com", False),
])
def test_matching_a_yc_page_to_its_company(slug, domain, ours):
    from harvey.collectors.profile import _is_their_yc_page
    assert _is_their_yc_page(slug, domain) is ours


@pytest.mark.parametrize("domain,junk", [
    ("workatastartup.com", True), ("startupschool.org", True), ("yclist.com", True),
    ("ycdb.co", True), ("seedtable.com", True), ("github.com", True),
    ("acme.github.io", False), ("angelcity.com", False),
])
def test_yc_lists_are_junk_and_github_pages_arent(domain, junk):
    assert is_junk(domain) is junk


def test_the_preset_excludes_ycs_own_sites():
    [q] = build_queries(_config(industries=["fintech"]), preset="yc")
    for site in ("ycombinator.com", "workatastartup.com", "startupschool.org"):
        assert f"-site:{site}" in q.term


@pytest.mark.asyncio
async def test_a_wrong_yes_from_profiling_is_undone_by_the_next_profile(state):
    await seed_signal_catalog(state)
    await state.set_signal_status("YC_BACKED", "confirmed")
    cid = await state.add_company(Company(name="Vendor", domain="vendor.com"))
    await state.add_observation(company_id=cid, signal_code="YC_BACKED", collector="profile",
                                value_num=1, observed_at="2026-01-01T00:00:00")
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed, c.site_names, c.state = await state.confirmed_signal_codes(), {}, state

    async def resolve(self, domain):
        return f"https://{domain}", "<p>Trusted by 300 YC-backed startups.</p>"
    c._resolve_site = resolve.__get__(c)

    async def get(url):
        return ""
    c._get = get
    [no] = [o for o in await c.profile_company(cid, "vendor.com") if o["signal_code"] == "YC_BACKED"]
    assert no["value_num"] == 0
    await state.add_observations([no])
    assert await state.yc_batch(cid) is None


@pytest.mark.asyncio
async def test_ycs_own_domains_are_never_profiled(state):
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed, c.site_names, c.state = {"YC_BACKED", "TECH_STACK"}, {}, state
    fetched = []

    async def resolve(self, domain):
        fetched.append(domain)
        return f"https://{domain}", "<p>x</p>"
    c._resolve_site = resolve.__get__(c)
    for domain in ("ycombinator.com", "www.ycombinator.com", "news.ycombinator.com", "workatastartup.com"):
        assert await c.profile_company("c", domain) == []
    assert fetched == []


@pytest.mark.asyncio
async def test_a_mixed_batch_doesnt_get_founder_framing(state):
    from harvey.agents.writer import Writer
    await seed_signal_catalog(state)
    yc = await _yc_company(state, True)
    plain = await _yc_company(state, False)
    brain = RecordingBrain()
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )
    w = Writer(brain, state, cfg)
    w.skills = ""
    await w._write_sequence([yc, plain])
    assert "SELLING TO STARTUP FOUNDERS" not in brain.prompts[0]



# ── second review: anchored claims ──


@pytest.mark.parametrize("html,domain", [
    ("<p>Smart, independent teams love it, backed by Y Combinator alumni.</p>", "art.com"),
    ("<p>Most went through Y Combinator and use it every day.</p>", "ever.com"),
    ("<p>Teams at Linear and Ramp are YC alumni.</p>", "linear.app"),
    ("<p>Notion is backed by Y Combinator? No, but Stripe is.</p>", "stripe.com"),
    ("<p>We sponsored the YC W24 batch dinner.</p>", "acme.com"),
    ("<p>Our team previously founded a YC-funded startup.</p>", "acme.com"),
    ("<p>We're hiring ex-YC alumni.</p>", "acme.com"),
    ("<h3>YC W24 recap</h3>", "acme.com"),
    ("<h3>YC-backed jobs</h3>", "acme.com"),
    ("<h3>YC alumni</h3>", "acme.com"),
    ("<p>Our podcast with YC alumni</p>", "acme.com"),
    ("<p>We believe in Y Combinator's mission.</p>", "acme.com"),
])
def test_mentions_that_arent_the_company_saying_it(html, domain):
    assert yc_backed(html, domain=domain) is None


@pytest.mark.parametrize("html,batch", [
    ("<p>Acme (YC S23) builds application security tools.</p>", "S23"),
    ("<p>Acme is backed by Y Combinator and used by 1,000 clinics.</p>", ""),
    ("<p>We're YC-backed and offer great perks.</p>", ""),
    ("<p>Acme is a YC-backed platform built for dentists.</p>", ""),
    ("<p>Backed by Y Combinator and trusted by 500 teams.</p>", ""),
    ("<p>We launched at YC Demo Day (W24).</p>", "W24"),
    ("<p>We were incubated at Y Combinator.</p>", ""),
])
def test_claims_with_other_words_nearby_still_count(html, batch):
    got = yc_backed(html, domain="acme.com")
    assert got is not None and got[0] == batch


@pytest.mark.parametrize("slug,domain", [("open", "openapp.com"), ("labs", "labsinc.com")])
def test_generic_slugs_dont_match_by_stripping_affixes(slug, domain):
    from harvey.collectors.profile import _is_their_yc_page
    assert _is_their_yc_page(slug, domain) is False


@pytest.mark.parametrize("html,domain,yes", [
    ("<p>Acme Corp was founded in 2021 and is backed by Y Combinator, General Catalyst and others.</p>",
     "acmecorp.com", True),
    ("<p>Founded in 2021, the company is backed by Y Combinator and General Catalyst.</p>", "acme.com", True),
    ("<p>Acme helps teams at companies that are backed by Y Combinator.</p>", "acme.com", False),
    ("<p>Acme works with startups which are funded by YC.</p>", "acme.com", False),
])
def test_third_person_claims_about_the_company(html, domain, yes):
    assert (yc_backed(html, domain=domain) is not None) is yes
