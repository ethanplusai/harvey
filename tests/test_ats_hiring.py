"""Open roles from the public job-board feeds a company's ATS publishes.

Greenhouse, Lever, Ashby and Workable each publish a company's open roles as
an unauthenticated JSON feed. Harvey finds which one a company uses from the
links on its OWN site (never by guessing tokens), reads that one feed, and
records the roles that match ``icp.hiring_signals`` (or ``icp.titles``).
"""

import json
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey.collectors.ats import (
    MAX_FEED_BYTES, Board, detect_board, feed_url, matching_roles, parse_feed,
    read_board, role_keywords,
)
from harvey.collectors.discover import (
    Business, DiscoveryProvider, DiscoveryQuery, PRESETS, _observations, build_queries, is_junk,
    preset_problem, run_discovery,
)
from harvey.collectors.profile import ProfileCollector, profile_companies
from harvey.models.company import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


# Shapes as the live feeds returned them (trimmed), October 2026.
GREENHOUSE = {"departments": [
    {"id": 1, "name": "Marketing", "parent_id": None, "child_ids": [], "jobs": [
        {"id": 11, "title": "Head of Growth", "location": {"name": "New York, NY"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
         "first_published": "2026-10-01T09:00:00-04:00", "updated_at": "2026-10-20T09:00:00-04:00"},
    ]},
    {"id": 2, "name": "Engineering", "parent_id": None, "child_ids": [], "jobs": [
        {"id": 12, "title": "Backend Engineer", "location": {"name": "Remote"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/12",
         "updated_at": "2026-09-02T09:00:00-04:00"},
    ]},
    {"id": 0, "name": "No Department", "parent_id": None, "child_ids": [], "jobs": [
        {"id": 13, "title": "Sales Development Representative", "location": {"name": "Austin, TX"},
         "absolute_url": "https://acme.com/careers?gh_jid=13",
         "first_published": "2026-10-10T09:00:00-04:00"},
        # Listed under two departments: counted once.
        {"id": 11, "title": "Head of Growth", "location": {"name": "New York, NY"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/11"},
    ]},
]}
LEVER = [
    {"id": "a1", "text": "VP of Marketing", "createdAt": 1790000000000,
     "categories": {"team": "Marketing", "location": "San Francisco, CA", "commitment": "Full-time"},
     "hostedUrl": "https://jobs.lever.co/acme/a1"},
    {"id": "a2", "text": "Account Executive", "createdAt": 1789000000000,
     "categories": {"department": "Sales", "location": "Remote"},
     "hostedUrl": "https://jobs.lever.co/acme/a2"},
]
ASHBY = {"apiVersion": "1", "jobs": [
    {"id": "x1", "title": " Growth Marketing Manager", "department": "Marketing", "team": "Growth",
     "location": "London", "publishedAt": "2026-10-05T17:12:35.753+00:00", "isListed": True,
     "jobUrl": "https://jobs.ashbyhq.com/acme/x1"},
    {"id": "x2", "title": "Unlisted Role", "department": "Ops", "location": "London",
     "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/x2"},
]}
WORKABLE = {"name": "Acme", "description": None, "jobs": [
    {"title": "Marketing Director", "department": "Marketing", "city": "Athens", "state": "Attica",
     "country": "Greece", "telecommuting": False, "published_on": "2026-10-12",
     "url": "https://apply.workable.com/j/ABC123"},
    {"title": "Support Agent", "department": "", "city": "", "country": "", "telecommuting": True,
     "published_on": "2026-08-01", "url": "https://apply.workable.com/j/DEF456"},
]}


# ── detection: from the company's own pages ──

@pytest.mark.parametrize("html, ats, token", [
    ('<a href="https://boards.greenhouse.io/acme">Careers</a>', "greenhouse", "acme"),
    ('<a href="https://boards.greenhouse.io/acme/jobs/4012345">Head of Growth</a>', "greenhouse", "acme"),
    ('<a href="https://job-boards.greenhouse.io/acme">Jobs</a>', "greenhouse", "acme"),
    ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>', "greenhouse", "acme"),
    ('<iframe src="https://job-boards.greenhouse.io/embed/job_board?for=acme&b=x"></iframe>', "greenhouse", "acme"),
    ('<a href="https://jobs.lever.co/acme">Open roles</a>', "lever", "acme"),
    ('<a href="https://jobs.lever.co/acme/5d1c-77/apply">Apply</a>', "lever", "acme"),
    ('<script>fetch("https://api.lever.co/v0/postings/acme?group=team&mode=json")</script>', "lever", "acme"),
    ('<a href="https://jobs.eu.lever.co/acme">Jobs</a>', "lever_eu", "acme"),
    ('<a href="https://jobs.ashbyhq.com/acme">Careers</a>', "ashby", "acme"),
    ('<script src="https://jobs.ashbyhq.com/acme/embed?version=2"></script>', "ashby", "acme"),
    ('{"careers":"https:\\/\\/jobs.ashbyhq.com\\/acme"}', "ashby", "acme"),
    ('<a href="https://apply.workable.com/acme/">Careers</a>', "workable", "acme"),
    ('<a href="https://acme.workable.com">Careers</a>', "workable", "acme"),
])
def test_boards_are_found_from_links_and_embeds(html, ats, token):
    board = detect_board(html, "acme.com")
    assert board is not None and (board.ats, board.token) == (ats, token)


@pytest.mark.parametrize("html", [
    "",
    "<p>We use Greenhouse to hire.</p>",                                   # a word, not a board
    '<a href="https://www.greenhouse.io/">Powered by Greenhouse</a>',      # the vendor's site
    '<a href="https://boards.greenhouse.io/embed/job_board">x</a>',        # no token
    '<a href="https://jobs.lever.co/">Lever</a>',
    '<a href="https://apply.workable.com/j/ABC123">One job</a>',           # a job shortlink, no account
    '<a href="https://www.workable.com/">Workable</a>',
    '<a href="https://jobs.ashbyhq.com/api/x">x</a>',
])
def test_no_board_without_a_real_board_link(html):
    assert detect_board(html, "acme.com") is None


def test_the_companys_own_board_wins_over_a_customers():
    html = ('<a href="https://jobs.lever.co/bigcustomer">Customer story</a>'
            '<a href="https://jobs.lever.co/bigcustomer/1">x</a>'
            '<a href="https://boards.greenhouse.io/acmehq">Careers</a>')
    board = detect_board(html, "acme.com")
    assert (board.ats, board.token) == ("greenhouse", "acmehq")


def test_feed_urls_are_the_documented_public_endpoints():
    assert feed_url(Board("greenhouse", "acme")) == "https://boards-api.greenhouse.io/v1/boards/acme/departments"
    assert feed_url(Board("lever", "acme")) == "https://api.lever.co/v0/postings/acme?mode=json"
    assert feed_url(Board("lever_eu", "acme")) == "https://api.eu.lever.co/v0/postings/acme?mode=json"
    assert feed_url(Board("ashby", "acme")) == "https://api.ashbyhq.com/posting-api/job-board/acme"
    assert feed_url(Board("workable", "acme")) == "https://apply.workable.com/api/v1/widget/accounts/acme"


# ── parsing each feed ──

def test_greenhouse_feed_parses_with_departments_and_dedups():
    posts = parse_feed("greenhouse", GREENHOUSE)
    assert [p.title for p in posts] == ["Head of Growth", "Backend Engineer",
                                        "Sales Development Representative"]
    growth = posts[0]
    assert (growth.department, growth.location, growth.posted) == ("Marketing", "New York, NY", "2026-10-01")
    assert posts[1].posted == "2026-09-02"                     # falls back to updated_at
    assert posts[2].department == ""                           # "No Department" isn't one
    assert growth.url == "https://boards.greenhouse.io/acme/jobs/11"


def test_lever_feed_parses():
    posts = parse_feed("lever", LEVER)
    assert [(p.title, p.department, p.location) for p in posts] == [
        ("VP of Marketing", "Marketing", "San Francisco, CA"),
        ("Account Executive", "Sales", "Remote")]
    assert posts[0].posted == "2026-09-21" and posts[0].url == "https://jobs.lever.co/acme/a1"


def test_ashby_feed_skips_unlisted_roles():
    posts = parse_feed("ashby", ASHBY)
    assert [(p.title, p.department, p.posted) for p in posts] == [
        ("Growth Marketing Manager", "Marketing", "2026-10-05")]


def test_workable_feed_parses():
    posts = parse_feed("workable", WORKABLE)
    assert [(p.title, p.location, p.posted) for p in posts] == [
        ("Marketing Director", "Athens, Attica, Greece", "2026-10-12"),
        ("Support Agent", "Remote", "2026-08-01")]


@pytest.mark.parametrize("ats, payload", [
    ("greenhouse", {"departments": "nope"}), ("greenhouse", []), ("lever", {"ok": False}),
    ("ashby", {"jobs": [None, 3, {"title": 5}]}), ("workable", None),
])
def test_odd_payloads_never_raise(ats, payload):
    assert parse_feed(ats, payload) == []


# ── which roles count ──

def test_role_keywords_prefer_hiring_signals_then_titles():
    icp = SimpleNamespace(hiring_signals=["Head of Growth", " "], titles=["CEO"])
    assert role_keywords(icp) == ["head of growth"]
    assert role_keywords(SimpleNamespace(hiring_signals=[], titles=["VP Marketing", "CEO"])) == [
        "vp marketing", "ceo"]
    assert role_keywords(SimpleNamespace(industries=["x"])) == []
    assert role_keywords(None) == []


def test_matching_is_by_whole_words_in_any_order():
    posts = parse_feed("lever", LEVER) + parse_feed("greenhouse", GREENHOUSE)
    hits = [p.title for p in matching_roles(posts, ["vp marketing", "head of growth"])]
    assert hits == ["VP of Marketing", "Head of Growth"]
    # "cto" is not inside "Director"; "sdr" isn't a word in the title.
    assert matching_roles(parse_feed("workable", WORKABLE), ["cto"]) == []
    assert [p.title for p in matching_roles(posts, ["sales"])] == ["Sales Development Representative"]
    assert matching_roles(posts, []) == []


# ── reading the feed politely ──

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_one_request_reads_the_board():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=LEVER)

    async with _client(handler) as client:
        posts = await read_board(client, Board("lever", "acme"))
    assert len(posts) == 2
    assert seen == ["https://api.lever.co/v0/postings/acme?mode=json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"ok": False}),
    httpx.Response(200, text="<html>not json</html>", headers={"content-type": "text/html"}),
    httpx.Response(200, content=b"{broken", headers={"content-type": "application/json"}),
])
async def test_a_missing_or_broken_board_reads_as_none(response):
    async with _client(lambda request: response) as client:
        assert await read_board(client, Board("ashby", "acme")) is None


@pytest.mark.asyncio
async def test_an_oversized_feed_is_abandoned():
    big = b'{"jobs": [' + b'{"title": "x"},' * (MAX_FEED_BYTES // 14) + b'{"title": "y"}]}'
    async with _client(lambda r: httpx.Response(
            200, content=big, headers={"content-type": "application/json"})) as client:
        assert await read_board(client, Board("ashby", "acme")) is None


@pytest.mark.asyncio
async def test_network_errors_read_as_none():
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    async with _client(handler) as client:
        assert await read_board(client, Board("greenhouse", "acme")) is None


# ── profiling: HIRING_ROLE + OPEN_ROLES from the board ──

def _collector(confirmed, pages: dict[str, str], feeds: dict[str, object], keywords=("head of growth",)):
    c = ProfileCollector(None, set(confirmed), role_keywords=list(keywords))
    requested = []

    async def fake_get(url):
        requested.append(url)
        return pages.get(url, "")

    def handler(request):
        requested.append(str(request.url))
        body = feeds.get(str(request.url))
        if body is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=body)

    c._get = fake_get
    c._client = _client(handler)
    c.requested = requested
    return c


HOME = "<html><body>" + "x" * 600 + "<a href='/careers'>Careers</a></body></html>"


@pytest.mark.asyncio
async def test_profile_reads_the_board_linked_from_the_homepage():
    home = HOME.replace("/careers", "https://boards.greenhouse.io/acme")
    c = _collector({"HIRING_ROLE", "OPEN_ROLES"}, {"https://acme.com": home},
                   {"https://boards-api.greenhouse.io/v1/boards/acme/departments": GREENHOUSE})
    obs = await c.profile_company("c1", "acme.com")
    hiring = [o for o in obs if o["signal_code"] == "HIRING_ROLE"]
    assert [o["value_text"] for o in hiring] == [
        "Head of Growth · Marketing · New York, NY · posted 2026-10-01"]
    assert hiring[0]["evidence_url"] == "https://boards.greenhouse.io/acme/jobs/11"
    [count] = [o for o in obs if o["signal_code"] == "OPEN_ROLES"]
    assert count["value_num"] == 3 and count["value_text"] == "Greenhouse"
    assert count["evidence_url"] == "https://boards.greenhouse.io/acme"
    # One feed request; no careers-page crawl once the homepage named the board.
    feed_calls = [u for u in c.requested if "greenhouse" in u]
    assert feed_calls == ["https://boards-api.greenhouse.io/v1/boards/acme/departments"]
    assert not any("/careers" in u or "/jobs" in u for u in c.requested if "acme.com" in u)


@pytest.mark.asyncio
async def test_profile_finds_the_board_on_the_careers_page():
    careers = "<html>" + "y" * 600 + '<a href="https://jobs.ashbyhq.com/acme">See roles</a></html>'
    c = _collector({"HIRING_ROLE"}, {"https://acme.com": HOME, "https://acme.com/careers": careers},
                   {"https://api.ashbyhq.com/posting-api/job-board/acme": ASHBY},
                   keywords=["growth marketing manager"])
    obs = await c.profile_company("c1", "acme.com")
    assert [o["value_text"] for o in obs] == ["Growth Marketing Manager · Marketing · London · posted 2026-10-05"]
    assert not any(o["signal_code"] == "OPEN_ROLES" for o in obs)      # not confirmed


@pytest.mark.asyncio
async def test_profile_follows_the_homepages_own_careers_link():
    home = HOME.replace("/careers", "https://careers.acme.com/open-roles")
    careers = "<html>" + "y" * 600 + '<a href="https://jobs.lever.co/acme">Roles</a></html>'
    c = _collector({"HIRING_ROLE"}, {"https://acme.com": home,
                                     "https://careers.acme.com/open-roles": careers},
                   {"https://api.lever.co/v0/postings/acme?mode=json": LEVER},
                   keywords=["vp marketing"])
    obs = await c.profile_company("c1", "acme.com")
    assert [o["value_text"] for o in obs] == ["VP of Marketing · Marketing · San Francisco, CA · posted 2026-09-21"]


@pytest.mark.asyncio
async def test_a_board_with_no_matching_role_records_only_the_count():
    home = HOME.replace("/careers", "https://jobs.lever.co/acme")
    c = _collector({"HIRING_ROLE", "OPEN_ROLES"}, {"https://acme.com": home},
                   {"https://api.lever.co/v0/postings/acme?mode=json": LEVER},
                   keywords=["chief of staff"])
    obs = await c.profile_company("c1", "acme.com")
    assert [(o["signal_code"], o["value_num"]) for o in obs] == [("OPEN_ROLES", 2), ("HIRING_ROLE", 0)]


@pytest.mark.asyncio
async def test_matched_roles_are_capped():
    many = [{"id": str(i), "text": f"Growth Lead {i}", "categories": {},
             "hostedUrl": f"https://jobs.lever.co/acme/{i}"} for i in range(20)]
    home = HOME.replace("/careers", "https://jobs.lever.co/acme")
    c = _collector({"HIRING_ROLE", "OPEN_ROLES"}, {"https://acme.com": home},
                   {"https://api.lever.co/v0/postings/acme?mode=json": many}, keywords=["growth"])
    obs = await c.profile_company("c1", "acme.com")
    assert len([o for o in obs if o["signal_code"] == "HIRING_ROLE"]) == 5
    assert [o["value_num"] for o in obs if o["signal_code"] == "OPEN_ROLES"] == [20]


@pytest.mark.asyncio
async def test_a_dead_board_falls_back_to_reading_the_careers_page():
    careers = ("<html>" + "y" * 600 + '<a href="https://jobs.lever.co/acme">Roles</a>'
               "<h3>Sales Manager</h3></html>")
    c = _collector({"HIRING_ROLE", "OPEN_ROLES"},
                   {"https://acme.com": HOME, "https://acme.com/careers": careers}, {})
    obs = await c.profile_company("c1", "acme.com")
    assert [(o["signal_code"], o["value_text"]) for o in obs] == [("HIRING_ROLE", "Sales Manager")]


@pytest.mark.asyncio
async def test_no_feed_request_unless_a_hiring_signal_is_confirmed():
    home = HOME.replace("/careers", "https://jobs.lever.co/acme")
    c = _collector({"TECH_STACK"}, {"https://acme.com": home},
                   {"https://api.lever.co/v0/postings/acme?mode=json": LEVER})
    await c.profile_company("c1", "acme.com")
    assert not any("lever" in u for u in c.requested)


@pytest.mark.asyncio
async def test_profile_companies_takes_keywords_from_the_config(state, monkeypatch):
    await seed_signal_catalog(state)
    await state.set_signal_status("HIRING_ROLE", "confirmed")
    seen = {}

    async def fake_run(self, companies, run_id=""):
        seen["keywords"] = self.role_keywords
        return 0

    monkeypatch.setattr(ProfileCollector, "run", fake_run)
    config = SimpleNamespace(icp=SimpleNamespace(hiring_signals=["Head of Growth"], titles=["CEO"]))
    await profile_companies(state, [], config=config)
    assert seen["keywords"] == ["head of growth"]


def test_open_roles_is_a_free_profile_signal():
    sig = next(s for s in SIGNAL_CATALOG if s["code"] == "OPEN_ROLES")
    assert sig["collector"] == "profile" and sig["value_type"] == "num"
    assert "free" in sig["cost_note"]
    hiring = next(s for s in SIGNAL_CATALOG if s["code"] == "HIRING_ROLE")
    assert "free" in hiring["cost_note"]


# ── discovery: the "hiring" preset ──

def _config(industries=("fintech",), titles=("CEO",), hiring=("Head of Growth", "SDR")):
    return SimpleNamespace(icp=SimpleNamespace(
        industries=list(industries), geography=["United States"], geo_coordinates={},
        titles=list(titles), hiring_signals=list(hiring)), product=SimpleNamespace(name="P"))


def test_the_hiring_preset_searches_company_careers_pages_for_each_role():
    queries = build_queries(_config(industries=("fintech", "dev tools")), preset="hiring")
    terms = [q.term for q in queries]
    assert len(terms) == 4 and all(q.location == "" and q.preset == "hiring" for q in queries)
    assert sum('"head of growth"' in t for t in terms) == 2 and sum('"sdr"' in t for t in terms) == 2
    # The hosted boards and the job aggregators are not the company.
    for site in ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "linkedin.com", "indeed.com"):
        assert all(f"-site:{site}" in t for t in terms)


def test_the_hiring_preset_falls_back_to_titles_and_needs_search():
    terms = [q.term for q in build_queries(_config(hiring=()), preset="hiring")]
    assert len(terms) == 1 and '"ceo"' in terms[0]
    assert preset_problem("hiring", "osm")
    assert preset_problem("hiring", "serper") is None
    assert "hiring" in PRESETS


def test_the_yc_preset_is_unchanged_by_roles():
    [q] = build_queries(_config(), preset="yc")
    assert "{role}" not in q.term and '"backed by Y Combinator" fintech' in q.term


@pytest.mark.parametrize("domain, junk", [
    ("boards.greenhouse.io", True), ("job-boards.greenhouse.io", True), ("jobs.lever.co", True),
    ("jobs.ashbyhq.com", True), ("apply.workable.com", True),
    ("greenhouse.io", False), ("lever.co", False), ("ashbyhq.com", False),
])
def test_hosted_job_boards_are_not_prospects(domain, junk):
    assert is_junk(domain) is junk


class _OneHit(DiscoveryProvider):
    key, label, kind = "fake", "Fake", "serp"

    def __init__(self, domain):
        self.domain = domain

    def configured(self, env):
        return True

    def estimate(self, queries):
        return 0.0

    async def fetch(self, client, env, query):
        return [Business(name="Acme", domain=self.domain, website=f"https://{self.domain}/jobs",
                         provider="fake", source_url=f"https://{self.domain}/jobs")]


@pytest.mark.asyncio
async def test_a_careers_subdomain_hit_is_recorded_as_the_company(state, monkeypatch):
    from harvey.collectors import discover as D
    monkeypatch.setitem(D.PROVIDERS, "fake", _OneHit("careers.acme.com"))
    monkeypatch.setattr("harvey.config.load_env", lambda: SimpleNamespace(model_dump=lambda: {}),
                        raising=False)
    await seed_signal_catalog(state)
    await state.set_signal_status("FOUND_IN_SERP", "confirmed")
    q = build_queries(_config(), preset="hiring")[0]
    report = await run_discovery(state, _config(), "fake", [q])
    assert report.new_companies == 1, report.errors
    assert await state.get_company_by_domain("acme.com") is not None
    assert await state.get_company_by_domain("careers.acme.com") is None


def test_a_hiring_preset_hit_is_not_itself_a_hiring_signal():
    q = DiscoveryQuery(term="x", preset="hiring")
    biz = Business(name="Acme", domain="acme.com", provider="fake")
    codes = {r["signal_code"] for r in _observations(biz, "c1", {"HIRING_ROLE", "FOUND_IN_SERP"}, "r1", query=q)}
    assert codes == {"FOUND_IN_SERP"}       # the feed confirms the role, not the search snippet


@pytest.mark.asyncio
async def test_a_product_page_about_hiring_is_not_the_careers_page():
    home = ("<html>" + "x" * 600 + '<a href="/scheduling/hiring-marketplaces">For hiring teams</a>'
            '<a href="/company/careers">Work here</a></html>')
    page = "<html>" + "y" * 600 + '<a href="https://jobs.lever.co/acme">Roles</a></html>'
    c = _collector({"OPEN_ROLES"}, {"https://acme.com": home,
                                    "https://acme.com/scheduling/hiring-marketplaces": page,
                                    "https://acme.com/company/careers": page},
                   {"https://api.lever.co/v0/postings/acme?mode=json": LEVER})
    await c.profile_company("c1", "acme.com")
    assert "https://acme.com/company/careers" in c.requested
    assert "https://acme.com/scheduling/hiring-marketplaces" not in c.requested


# ── review fixes ──

@pytest.mark.asyncio
async def test_a_board_with_no_matching_role_says_not_hiring():
    home = HOME.replace("/careers", "https://jobs.lever.co/acme")
    c = _collector({"HIRING_ROLE"}, {"https://acme.com": home},
                   {"https://api.lever.co/v0/postings/acme?mode=json": LEVER},
                   keywords=["chief of staff"])
    obs = await c.profile_company("c1", "acme.com")
    assert [(o["signal_code"], o["value_num"], o["value_text"]) for o in obs] == [("HIRING_ROLE", 0, "")]
    assert obs[0]["evidence_url"] == "https://jobs.lever.co/acme"


@pytest.mark.asyncio
async def test_a_closed_role_stops_being_current(state):
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name="Acme", domain="acme.com"))
    await state.add_observations([{
        "company_id": cid, "signal_code": "HIRING_ROLE", "collector": "profile",
        "value_text": "Head of Growth · Marketing", "observed_at": "2026-09-01 10:00:00"}], run_id="r1")
    assert await state.cohort(["HIRING_ROLE"]) == [cid]
    # Next month the board no longer lists a matching role.
    await state.add_observations([{
        "company_id": cid, "signal_code": "HIRING_ROLE", "collector": "profile",
        "value_num": 0, "value_text": "", "observed_at": "2026-10-01 10:00:00"}], run_id="r2")
    assert await state.cohort(["HIRING_ROLE"]) == []
    assert not any(r["signal_code"] == "HIRING_ROLE" for r in await state.signal_counts())


@pytest.mark.asyncio
async def test_a_failing_board_read_keeps_the_other_signals(monkeypatch):
    import harvey.collectors.profile as P
    home = HOME.replace("/careers", "https://jobs.lever.co/acme") + '<script src="https://js.stripe.com/v3"></script>'

    async def boom(client, board):
        raise RecursionError("deep")

    monkeypatch.setattr(P, "read_board", boom)
    c = _collector({"HIRING_ROLE", "OPEN_ROLES", "TECH_STACK"}, {"https://acme.com": home}, {})
    obs = await c.profile_company("c1", "acme.com")
    assert [o["signal_code"] for o in obs] == ["TECH_STACK"]


@pytest.mark.asyncio
async def test_deeply_nested_json_reads_as_none(monkeypatch):
    # Deep nesting makes json.loads raise RecursionError on 3.11/3.12; newer
    # Pythons parse it. Simulate the raise so every version checks the catch.
    import harvey.collectors.ats as ats

    def too_deep(_):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ats.json, "loads", too_deep)
    async with _client(lambda r: httpx.Response(
            200, content=b"[[[[]]]]", headers={"content-type": "application/json"})) as client:
        assert await read_board(client, Board("lever", "acme")) is None


def test_encoded_tokens_are_decoded_once():
    board = detect_board('<a href="https://jobs.ashbyhq.com/Acme%20Inc">Jobs</a>', "acme.com")
    assert board.token == "Acme Inc"
    assert feed_url(board) == "https://api.ashbyhq.com/posting-api/job-board/Acme%20Inc"


@pytest.mark.parametrize("html", [
    '<script src="https://boards.greenhouse.io/embed.js"></script>',
    '<script src="https://jobs.ashbyhq.com/embed.js"></script>',
    '<script src="https://jobs.lever.co/widget.min.js"></script>',
    '<a href="mailto:jobs@acme.workable.com">Email us</a>',
    "<p>Write to careers@acme.workable.com</p>",
])
def test_asset_names_and_emails_are_not_boards(html):
    assert detect_board(html, "acme.com") is None


def test_greenhouse_job_app_embed_is_a_board():
    html = '<iframe src="https://boards.greenhouse.io/embed/job_app?for=acme&token=123"></iframe>'
    assert detect_board(html, "acme.com") == Board("greenhouse", "acme")


def test_a_page_listing_many_boards_is_nobodys():
    html = ('<a href="https://jobs.lever.co/portco1">1</a>'
            '<a href="https://boards.greenhouse.io/portco2">2</a>'
            '<a href="https://jobs.ashbyhq.com/portco3">3</a>')
    assert detect_board(html, "bigvc.com") is None
    # ...unless one of them is named like the site.
    assert detect_board(html + '<a href="https://jobs.lever.co/bigvc">us</a>', "bigvc.com") == Board("lever", "bigvc")
    # Two boards is still a choice (a customer story plus their own).
    assert detect_board(html.split("<a href=\"https://jobs.ashbyhq")[0], "x.com") is not None


@pytest.mark.parametrize("keyword, title, hit", [
    ("vp marketing", "Vice President, Marketing", True),
    ("vice president of sales", "VP Sales", True),
    ("cmo", "Chief Marketing Officer", True),
    ("chief technology officer", "CTO", True),
    ("senior account executive", "Sr. Account Executive", True),
    ("frontend engineer", "Front-End Engineer", True),
    ("ceo", "Executive Assistant to the CEO", False),
    ("ceo", "Chief of Staff to the CEO", False),
    ("vp marketing", "Assistant to the VP of Marketing", False),
    ("ceo", "CEO", True),
])
def test_role_equivalences_and_assistants(keyword, title, hit):
    assert bool(matching_roles([parse_feed("lever", [{"text": title}])[0]], [keyword])) is hit


def test_the_hiring_preset_needs_roles():
    empty = _config(titles=(), hiring=())
    problem = preset_problem("hiring", "serper", config=empty)
    assert problem and "hiring_signals" in problem
    assert preset_problem("hiring", "serper", config=_config()) is None
    assert preset_problem("yc", "serper", config=empty) is None


@pytest.mark.parametrize("domain", [
    "jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com", "jobs.smartrecruiters.com",
    "jobs.jobvite.com", "jobs.co.uk", "careers.co.uk", "careers-acme.icims.com", "acme.bamboohr.com",
    "acme.recruitee.com", "acme.teamtailor.com", "acme.jobs.personio.de", "acme.breezy.hr",
    "acme.applytojob.com", "ats.rippling.com", "acme.wd5.myworkdayjobs.com",
])
@pytest.mark.asyncio
async def test_hiring_preset_hits_on_ats_hosts_are_never_prospects(state, monkeypatch, domain):
    from harvey.collectors import discover as D
    monkeypatch.setitem(D.PROVIDERS, "fake", _OneHit(domain))
    monkeypatch.setattr("harvey.config.load_env", lambda: SimpleNamespace(model_dump=lambda: {}),
                        raising=False)
    await seed_signal_catalog(state)
    await state.set_signal_status("FOUND_IN_SERP", "confirmed")
    report = await run_discovery(state, _config(), "fake", build_queries(_config(), preset="hiring")[:1])
    assert report.new_companies == 0 and report.junk == 1


def test_the_hiring_preset_excludes_other_ats_hosts():
    [q] = build_queries(_config(hiring=("SDR",)), preset="hiring")
    for site in ("smartrecruiters.com", "jobvite.com", "icims.com", "bamboohr.com", "myworkdayjobs.com"):
        assert f"-site:{site}" in q.term
