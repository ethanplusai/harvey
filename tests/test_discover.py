"""DISCOVER — junk filtering, entity resolution, cost discipline, adapters.

The expensive stage, so the tests care most about the things that cost money
or corrupt the database: that an estimate is honest, that a budget is
enforced before spending rather than after, that a re-run doesn't duplicate
anyone, and that directories never make it in.
"""

import asyncio
import json
import os
from contextlib import closing
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.config import EnvConfig, load_config
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.fixture
def config():
    return load_config()


# The real implementations, captured before the autouse fixture stubs them,
# so the geocoding tests can exercise them directly.
REAL_GEOCODE = D.geocode_city
REAL_RESOLVE = D.resolve_coordinates
REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test reaches the network, and none of them wait a real second.

    The geocoding tests call REAL_GEOCODE / REAL_RESOLVE with their own
    mock-transport client.
    """
    async def no_geocode(state, city, radius_km=40, client=None):
        return ""

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(D, "geocode_city", no_geocode)
    monkeypatch.setattr(D.asyncio, "sleep", no_sleep)


class FakeProvider(D.DiscoveryProvider):
    """A provider that returns what the test hands it, and spends nothing."""

    key = "fake"
    label = "Fake"
    cost_note = "free"

    def __init__(self, batches, per_query_cost=0.0):
        self.batches = list(batches)
        self.per_query_cost = per_query_cost
        self.calls = 0

    def configured(self, env):
        return True

    def estimate(self, queries):
        return self.per_query_cost * len(queries)

    async def fetch(self, client, env, query):
        self.calls += 1
        self.last_cost = self.per_query_cost
        return self.batches.pop(0) if self.batches else []


@pytest.fixture
def register(monkeypatch):
    """Install a fake provider into the registry for one test."""
    def _install(provider):
        monkeypatch.setitem(D.PROVIDERS, provider.key, provider)
        return provider
    return _install


def biz(**kw):
    kw.setdefault("provider", "fake")
    return D.Business(**kw)


# ── Junk filtering ──


@pytest.mark.parametrize("domain", [
    "yelp.com", "www.yellowpages.com", "angi.com", "homeadvisor.com",
    "facebook.com", "linkedin.com", "thumbtack.com", "houzz.com",
    "en.wikipedia.org", "reddit.com", "indeed.com",
    "miami.edu", "ufhealth.org", "umiamihealth.org",
    "somehospital.com", "mysite.wordpress.com", "shop.shopify.com",
    "192.168.1.1", "localhost",
])
def test_directories_and_institutions_are_junk(domain):
    assert D.is_junk(domain), f"{domain} should have been filtered out"


@pytest.mark.parametrize("domain", [
    "kettlemanroofing.com", "northvaleroofing.com", "sandpiperexteriors.com",
    "quarrylaneroofing.com", "smith-and-sons.co.uk", "acme.dental",
])
def test_real_businesses_survive_the_filter(domain):
    assert not D.is_junk(domain)


def test_listicle_titles_are_junk_even_with_a_clean_domain():
    assert D.is_junk("", "10 Best Roofers in Denver")
    assert D.is_junk("example.com", "Top Roofing Contractors in Dallas")
    assert D.is_junk("example.com", "Roofer Directory")
    assert not D.is_junk("example.com", "Northvale Roofing Company")
    assert D.is_junk("example.net", "Ohio HOA Management Company List")
    assert D.is_junk("example.com", "List of Plumbers in Austin")
    assert not D.is_junk("example.com", "Northvale Management")


def test_normalize_domain_is_the_entity_key():
    for raw in ("https://WWW.Example.com/path?q=1", "www.example.com",
                "http://example.com.", "EXAMPLE.COM"):
        assert D.normalize_domain(raw) == "example.com"
    assert D.normalize_domain("") == ""


# ── Cost discipline ──


def test_estimates_match_published_pricing():
    q = [D.DiscoveryQuery(term="roofer", location="Denver", limit=1000, depth=30)]
    # DataForSEO's own worked example: 1,000 items = $0.012 + $0.36 = $0.372
    assert D.estimate_cost("dataforseo_listings", q) == pytest.approx(0.372)
    # SERP billing is the Live price x pages; depth 30 is three pages.
    assert D.estimate_cost("dataforseo_serp", q) == pytest.approx(0.006)
    assert D.estimate_cost("osm", q) == 0.0
    # Scavio: 1 credit a page at the $0.01 pay-as-you-go price. Maps
    # pages hold 20 and stop at six; Search pages hold 10.
    assert D.estimate_cost("scavio_maps", q) == pytest.approx(6 * 0.01)
    assert D.estimate_cost("scavio_search", q) == pytest.approx(3 * 0.01)


@pytest.mark.asyncio
async def test_dry_run_estimates_without_calling_anything(state, config, register):
    fake = register(FakeProvider([[biz(domain="a.com")]], per_query_cost=0.5))
    q = [D.DiscoveryQuery(term="roofer", location="Denver")]
    report = await D.run_discovery(state, config, "fake", q, dry_run=True)
    assert report.estimated_cost == 0.5
    assert fake.calls == 0
    assert report.found == 0
    assert not await state.get_runs()   # a dry run isn't a run


@pytest.mark.asyncio
async def test_budget_cap_refuses_before_spending(state, config, register):
    fake = register(FakeProvider([[biz(domain="a.com")]] * 10, per_query_cost=1.0))
    q = [D.DiscoveryQuery(term="roofer", location=c) for c in "abcdefghij"]
    report = await D.run_discovery(state, config, "fake", q, max_spend=2.0)
    assert report.stopped == "over budget"
    assert fake.calls == 0, "the cap must be checked before the first call, not after"
    assert report.actual_cost == 0.0


@pytest.mark.asyncio
async def test_budget_stops_mid_run_when_actual_exceeds_estimate(state, config, register):
    # Cheap to estimate, expensive in reality — the loop has to notice.
    fake = FakeProvider([[biz(domain=f"{c}.com")] for c in "abcdefghij"],
                        per_query_cost=1.0)
    fake.estimate = lambda queries: 0.0
    register(fake)
    q = [D.DiscoveryQuery(term="roofer", location=c) for c in "abcdefghij"]
    report = await D.run_discovery(state, config, "fake", q, max_spend=3.0)
    assert report.stopped == "budget reached"
    assert fake.calls < 10


@pytest.mark.asyncio
async def test_kill_switch_stops_between_batches(state, config, register):
    await state.set_setting("discovery_paused", "stopped by hand")
    fake = register(FakeProvider([[biz(domain="a.com")]], per_query_cost=0.0))
    q = [D.DiscoveryQuery(term="roofer", location="Denver")]
    report = await D.run_discovery(state, config, "fake", q)
    assert report.stopped == "paused"
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_nothing_runs_until_signals_are_confirmed(state, config, register):
    for sig in SIGNAL_CATALOG:
        await state.set_signal_status(sig["code"], "proposed")
    fake = register(FakeProvider([[biz(domain="a.com")]]))
    report = await D.run_discovery(state, config, "fake",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.stopped == "no confirmed signals"
    assert fake.calls == 0
    assert "confirm signals" in report.errors[0].lower()


# ── Entity resolution ──


@pytest.mark.asyncio
async def test_rerun_finds_everyone_already_known(state, config, register):
    batch = [
        biz(name="Northvale Roofing", domain="northvaleroofing.com"),
        biz(name="No Site Roofing", external_id="osm:node/1", has_website=False),
    ]
    register(FakeProvider([list(batch), list(batch)]))
    q = [D.DiscoveryQuery(term="roofer", location="Denver")]

    first = await D.run_discovery(state, config, "fake", q)
    assert (first.new_companies, first.known_companies) == (2, 0)

    second = await D.run_discovery(state, config, "fake", q)
    assert (second.new_companies, second.known_companies) == (0, 2), (
        "a re-run must recognise both the domain-keyed and the "
        "external-id-keyed record")


@pytest.mark.asyncio
async def test_websiteless_businesses_are_kept_and_deduped(state, config, register):
    """The best prospect for someone selling websites has no website, so
    `domain` can't be the only identity key."""
    register(FakeProvider([
        [biz(name="A", external_id="osm:node/1", has_website=False),
         biz(name="B", external_id="osm:node/2", has_website=False),
         biz(name="A again", external_id="osm:node/1", has_website=False)],
    ]))
    report = await D.run_discovery(state, config, "fake",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.new_companies == 2
    assert len(await state.cohort(["NO_WEBSITE"])) == 2


@pytest.mark.asyncio
async def test_duplicates_within_one_run_are_collapsed(state, config, register):
    register(FakeProvider([[
        biz(domain="northvale.com"), biz(domain="northvale.com"), biz(domain="other.com"),
    ]]))
    report = await D.run_discovery(state, config, "fake",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.found == 3
    assert report.new_companies == 2


@pytest.mark.asyncio
async def test_junk_is_counted_not_silently_dropped(state, config, register):
    register(FakeProvider([[
        biz(domain="yelp.com"), biz(domain="angi.com"), biz(domain="real.com"),
    ]]))
    report = await D.run_discovery(state, config, "fake",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.junk == 2
    assert report.new_companies == 1


# ── Observations ──


@pytest.mark.asyncio
async def test_every_fact_becomes_an_observation(state, config, register):
    register(FakeProvider([[biz(
        name="Northvale", domain="northvale.com", rank=14, rating=4.6,
        review_count=88, is_claimed=False, has_website=True,
    )]]))
    await D.run_discovery(state, config, "fake", [D.DiscoveryQuery(term="roofer")])

    rows = {o["signal_code"]: o for o in await state.get_observations()}
    assert rows["SERP_RANK"]["value_num"] == 14
    assert rows["REVIEW_RATING"]["value_num"] == pytest.approx(4.6)
    assert rows["REVIEW_COUNT"]["value_num"] == 88
    assert "UNCLAIMED_LISTING" in rows
    assert "NO_WEBSITE" not in rows
    assert rows["FOUND_IN_SERP"]["collector"] == "discover"


@pytest.mark.asyncio
async def test_unconfirmed_signals_are_not_recorded(state, config, register):
    """The confirmation gate has to hold inside the collector, not just in
    the UI that sets it."""
    await state.set_signal_status("REVIEW_RATING", "rejected")
    register(FakeProvider([[biz(domain="northvale.com", rating=4.6, review_count=88)]]))
    await D.run_discovery(state, config, "fake", [D.DiscoveryQuery(term="roofer")])

    codes = {o["signal_code"] for o in await state.get_observations()}
    assert "REVIEW_RATING" not in codes
    assert "REVIEW_COUNT" in codes


@pytest.mark.asyncio
async def test_a_failed_query_does_not_lose_the_successful_ones(state, config, register):
    class Flaky(FakeProvider):
        async def fetch(self, client, env, query):
            self.calls += 1
            if self.calls == 2:
                raise httpx.ConnectError("boom")
            return [biz(domain=f"co{self.calls}.com")]

    register(Flaky([], per_query_cost=0.0))
    q = [D.DiscoveryQuery(term="roofer", location=c) for c in ("a", "b", "c")]
    report = await D.run_discovery(state, config, "fake", q)

    assert report.new_companies == 2
    assert len(report.errors) == 1 and "boom" in report.errors[0]
    runs = await state.get_runs()
    assert runs[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_run_is_logged_with_what_it_actually_cost(state, config, register):
    register(FakeProvider([[biz(domain="a.com")], [biz(domain="b.com")]],
                          per_query_cost=0.25))
    q = [D.DiscoveryQuery(term="roofer", location=c) for c in ("a", "b")]
    report = await D.run_discovery(state, config, "fake", q, max_spend=5.0)

    runs = await state.get_runs()
    assert runs[0]["stage"] == "discover"
    assert runs[0]["records"] == 2
    assert runs[0]["cost_usd"] == pytest.approx(0.5)
    assert report.actual_cost == pytest.approx(0.5)


# ── Provider adapters (parsing, against real response shapes) ──


DFS_LISTINGS = {
    "status_code": 20000,
    "cost": 0.0132,
    "tasks": [{"status_code": 20000, "result": [{"items": [
        {
            "title": "Northvale Roofing Company", "category": "Roofing contractor",
            "place_id": "ChIJabc123", "phone": "+15550100199",
            "url": "https://www.northvaleroofing.com/", "domain": "northvaleroofing.com",
            "address": "1400 Example Row, Raleigh, NC",
            "address_info": {"city": "Raleigh", "region": "North Carolina"},
            "is_claimed": True,
            "rating": {"value": 4.4, "votes_count": 512, "rating_max": 5},
            "check_url": "https://google.com/maps?cid=1",
        },
        {
            "title": "No Website Roofing", "category": "Roofing contractor",
            "place_id": "ChIJxyz789", "phone": "+15550100377",
            "url": None, "domain": None,
            "address_info": {"city": "Denver", "region": "Colorado"},
            "is_claimed": False,
            "rating": {"value": 4.9, "votes_count": 31, "rating_max": 5},
        },
    ]}]}],
}

DFS_SERP = {
    "status_code": 20000,
    "cost": 0.0018,
    "tasks": [{"status_code": 20000, "result": [{"items": [
        {"type": "organic", "rank_group": 1, "rank_absolute": 3,
         "position": "left", "domain": "yelp.com",
         "url": "https://yelp.com/denver", "title": "Best Roofers"},
        {"type": "organic", "rank_group": 14, "rank_absolute": 18,
         "position": "left", "domain": "kettlemanroofing.com",
         "url": "https://kettlemanroofing.com/", "title": "Kettleman Roofing"},
        {"type": "paid", "rank_group": 1, "domain": "ads.example.com"},
        {"type": "people_also_ask", "rank_group": 2},
    ]}]}],
}

SERPER_RESPONSE = {"organic": [
    {"title": "Kettleman Roofing", "link": "https://www.kettlemanroofing.com/",
     "position": 3},
    {"title": "Angi", "link": "https://www.angi.com/companylist/denver/roofing",
     "position": 4},
]}

TINYFISH_RESPONSE_P0 = {
    "query": "roofer Denver",
    "results": [
        {
            "position": 1,
            "site_name": "summitroofing.example",
            "title": "Summit Roofing — Denver Roof Replacement",
            "snippet": "Trusted local roofers serving Denver metro...",
            "url": "https://summitroofing.example/services",
        },
        {
            "position": 2,
            "site_name": "directory.example",
            "title": "Top Roofers in Denver | Directory",
            "snippet": "Find the best roofing pros...",
            "url": "https://www.directory.example/companylist/denver/roofing",
        },
    ],
    "total_results": 2,
    "page": 0,
}

TINYFISH_RESPONSE_P1 = {
    "query": "roofer Denver",
    "results": [
        {
            "position": 3,
            "site_name": "apexroofing.example",
            "title": "Apex Roofing | Denver CO",
            "snippet": "Commercial and residential roofing...",
            "url": "https://apexroofing.example",
        },
    ],
    "total_results": 3,
    "page": 1,
}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_dataforseo_listings_adapter():
    provider = D.DataForSEOListings()
    env = {"dataforseo_login": "u", "dataforseo_password": "p"}
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=DFS_LISTINGS)

    async with _client(handler) as client:
        out = await provider.fetch(client, env, D.DiscoveryQuery(
            term="roofing contractor", location="Denver, CO",
            coordinate="39.7,-104.9,50"))

    assert seen["auth"].startswith("Basic ")
    assert provider.last_cost == pytest.approx(0.0132)

    northvale, nosite = out
    assert northvale.domain == "northvaleroofing.com"
    assert northvale.phone == "+15550100199"
    assert northvale.location == "Raleigh, North Carolina"
    assert northvale.rating == 4.4 and northvale.review_count == 512
    assert northvale.has_website is True
    assert northvale.external_id == "dataforseo:ChIJabc123"

    # The websiteless record is the valuable one — it must survive intact.
    assert nosite.domain == "" and nosite.has_website is False
    assert nosite.is_claimed is False
    assert nosite.identity() == "dataforseo:ChIJxyz789"


@pytest.mark.asyncio
async def test_dataforseo_serp_adapter_reads_rank_group_not_position():
    """`position` in this payload means left/right column, not rank. Reading
    it as rank would score every result identically."""
    provider = D.DataForSEOSerp()
    async with _client(lambda r: httpx.Response(200, json=DFS_SERP)) as client:
        out = await provider.fetch(
            client, {"dataforseo_login": "u", "dataforseo_password": "p"},
            D.DiscoveryQuery(term="roofer", location="Denver", depth=30))

    assert [b.rank for b in out] == [1, 14], "paid and PAA rows must be dropped"
    assert out[1].domain == "kettlemanroofing.com"


@pytest.mark.asyncio
async def test_serper_adapter():
    provider = D.Serper()
    async with _client(lambda r: httpx.Response(200, json=SERPER_RESPONSE)) as client:
        out = await provider.fetch(client, {"serper_api_key": "k"},
                                   D.DiscoveryQuery(term="roofer", location="Denver"))
    assert out[0].domain == "kettlemanroofing.com" and out[0].rank == 3
    # Junk filtering happens in the run, so the adapter still returns Angi.
    assert out[1].domain == "angi.com"


def _scavio_maps_page(start, n):
    return {"local_results": [
        {"position": i + 1, "title": f"Roofer {start + i}",
         "place_id": f"ChIJ{start + i}", "website": f"https://roofer{start + i}.example/",
         "phone": "(303) 555-0100", "address": "1 Main St, Denver, CO 80202",
         "type": "Roofing contractor", "rating": 4.8, "reviews": 120}
        for i in range(n)], "credits_used": 1}


def _scavio_serp_page(start, n, last=False):
    return {"organic_results": [
        {"position": i + 1, "title": f"Roofer {start + i} | Denver",
         "link": f"https://www.roofer{start + i}.example/"}
        for i in range(n)], "credits_used": 1,
        "pagination": {"current": start // 10 + 1} if last else
                      {"current": start // 10 + 1, "next": f"https://google.com/search?start={start + 10}"}}


@pytest.mark.asyncio
async def test_scavio_maps_adapter():
    provider = D.ScavioMaps()
    seen = []
    payload = {"local_results": [
        {"position": 1, "title": "Kettleman Roofing", "place_id": "ChIJabc123",
         "website": "https://www.kettlemanroofing.com/?utm_source=gbp",
         "phone": "(303) 555-0199", "address": "12 Grant St, Denver, CO 80203",
         "type": "Roofing contractor", "rating": 4.9, "reviews": 409},
        {"position": 2, "title": "No Site Roofing", "place_id": "ChIJxyz789",
         "type": "Roofing contractor", "rating": 5, "reviews": 12},
    ], "credits_used": 1}

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization"),
                     json.loads(request.content)))
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        out = await provider.fetch(client, {"scavio_api_key": "k"}, D.DiscoveryQuery(
            term="roofing contractor", location="Denver, CO",
            coordinate="39.7392,-104.9903,40"))

    url, auth, body = seen[0]
    assert url == "https://api.scavio.dev/api/v2/google/maps/search"
    assert auth == "Bearer k"
    # The map center picks the city; the term alone is the search.
    assert body == {"query": "roofing contractor", "ll": "@39.7392,-104.9903,10z",
                    "start": 0}
    assert len(seen) == 1, "a short page is the last page"
    assert provider.last_cost == pytest.approx(0.01)

    kettleman, nosite = out
    assert kettleman.domain == "kettlemanroofing.com"
    assert kettleman.phone == "(303) 555-0199"
    assert kettleman.category == "Roofing contractor"
    assert kettleman.rating == 4.9 and kettleman.review_count == 409
    assert kettleman.external_id == "gmaps:ChIJabc123"
    # A Maps list position is not an organic rank; recording it as one would
    # pollute SERP_RANK. Claimed status is unknown, not False.
    assert kettleman.rank is None and kettleman.is_claimed is None
    assert nosite.domain == "" and nosite.has_website is False
    assert nosite.identity() == "gmaps:ChIJxyz789"


@pytest.mark.asyncio
async def test_scavio_maps_without_a_coordinate_searches_by_place_name():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"local_results": []})

    async with _client(handler) as client:
        out = await D.ScavioMaps().fetch(client, {"scavio_api_key": "k"},
                                         D.DiscoveryQuery(term="roofer", location="Denver, CO"))
    assert out == []
    assert bodies == [{"query": "roofer Denver, CO", "start": 0}]


@pytest.mark.parametrize("coordinate,center", [
    ("39.7392,-104.9903,40", "@39.7392,-104.9903,10z"),
    ("39.7392,-104.9903,5", "@39.7392,-104.9903,13z"),
    ("39.7392,-104.9903", "@39.7392,-104.9903,10z"),
    ("39.7392,-104.9903,0.01", "@39.7392,-104.9903,16z"),
    ("39.7392,-104.9903,99999", "@39.7392,-104.9903,3z"),
    ("", ""),
])
def test_scavio_maps_turns_a_radius_into_a_zoom(coordinate, center):
    assert D.ScavioMaps.map_center(coordinate) == center


@pytest.mark.asyncio
async def test_scavio_maps_pages_up_to_the_limit():
    starts = []

    def handler(request):
        start = json.loads(request.content)["start"]
        starts.append(start)
        return httpx.Response(200, json=_scavio_maps_page(start, 20))

    provider = D.ScavioMaps()
    async with _client(handler) as client:
        out = await provider.fetch(client, {"scavio_api_key": "k"},
                                   D.DiscoveryQuery(term="roofer", coordinate="39.7,-104.9,40",
                                                    limit=50))
    assert starts == [0, 20, 40]
    assert len(out) == 60
    assert provider.last_cost == pytest.approx(3 * 0.01)

    # The API stops at start=100, so a huge limit is six pages, not fifty.
    starts.clear()
    async with _client(handler) as client:
        await provider.fetch(client, {"scavio_api_key": "k"},
                             D.DiscoveryQuery(term="roofer", coordinate="39.7,-104.9,40",
                                              limit=1000))
    assert starts == [0, 20, 40, 60, 80, 100]


@pytest.mark.asyncio
async def test_scavio_search_rank_continues_across_pages():
    """`position` restarts at 1 on every page. Page 2's first result is rank 11;
    reading it as rank 1 would put the businesses at 11-30 on page one."""
    def handler(request):
        start = json.loads(request.content)["start"]
        return httpx.Response(200, json=_scavio_serp_page(start, 10))

    provider = D.ScavioSearch()
    async with _client(handler) as client:
        out = await provider.fetch(client, {"scavio_api_key": "k"},
                                   D.DiscoveryQuery(term="roofer", location="Denver", depth=30))
    assert [b.rank for b in out] == list(range(1, 31))
    assert out[10].domain == "roofer10.example"
    assert provider.last_cost == pytest.approx(3 * 0.01)


@pytest.mark.asyncio
async def test_scavio_search_short_page_is_not_the_last_page():
    """Google often shows 7-9 organic results on page one (a local pack takes
    the space). Stopping there would cut a depth-30 search to one page."""
    starts = []

    def handler(request):
        start = json.loads(request.content)["start"]
        starts.append(start)
        return httpx.Response(200, json=_scavio_serp_page(start, 8, last=start == 10))

    async with _client(handler) as client:
        out = await D.ScavioSearch().fetch(client, {"scavio_api_key": "k"},
                                           D.DiscoveryQuery(term="roofer", depth=30))
    assert starts == [0, 10], "page 2 has no next link, so page 3 is never bought"
    assert [b.rank for b in out] == list(range(1, 9)) + list(range(11, 19))


@pytest.mark.asyncio
async def test_scavio_keeps_earlier_pages_when_a_later_page_fails():
    def handler(request):
        start = json.loads(request.content)["start"]
        if start:
            return httpx.Response(502, json={"error": "upstream timeout"})
        return httpx.Response(200, json=_scavio_serp_page(0, 10))

    provider = D.ScavioSearch()
    async with _client(handler) as client:
        out = await provider.fetch(client, {"scavio_api_key": "k"},
                                   D.DiscoveryQuery(term="roofer", depth=30))
    assert len(out) == 10
    # Only the page that answered is billed.
    assert provider.last_cost == pytest.approx(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [D.ScavioMaps, D.ScavioSearch])
async def test_scavio_errors_surface_the_message_not_just_the_code(cls):
    """A first-page failure raises: an empty list would make an expired key or
    an empty balance look like a market with nobody in it."""
    payload = {"error": "Insufficient credits"}
    with pytest.raises(RuntimeError, match="Scavio 402: insufficient credits"):
        async with _client(lambda r: httpx.Response(402, json=payload)) as client:
            await cls().fetch(client, {"scavio_api_key": "k"},
                              D.DiscoveryQuery(term="roofer", coordinate="39.7,-104.9,40"))


@pytest.mark.asyncio
async def test_tinyfish_adapter():
    provider = D.TinyFish()
    assert provider.estimate([D.DiscoveryQuery(term="roofer")]) == 0.0

    def handler(request):
        assert request.headers.get("x-api-key") == "tf_key"
        assert request.headers.get("accept") == "application/json"
        assert request.url.params.get("query") == "roofer Denver"
        return httpx.Response(200, json=TINYFISH_RESPONSE_P0)

    async with _client(handler) as client:
        out = await provider.fetch(client, {"tinyfish_api_key": "tf_key"},
                                   D.DiscoveryQuery(term="roofer", location="Denver", depth=10))

    assert len(out) == 2
    assert out[0].domain == "summitroofing.example"
    # TinyFish's own ordering, from no location: not a Google rank, so it is
    # never recorded as SERP_RANK next to Serper's and DataForSEO's.
    assert out[0].rank is None
    assert out[0].provider == "tinyfish"
    assert out[0].website == "https://summitroofing.example/services"
    assert out[0].keyword == "roofer Denver"
    assert out[1].domain == "directory.example"


@pytest.mark.asyncio
async def test_tinyfish_adapter_pagination():
    provider = D.TinyFish()
    pages_called = []

    def handler(request):
        page = request.url.params.get("page")
        pages_called.append(page)
        if page == "0":
            items = [{"position": i + 1, "url": f"https://biz{i}.example", "title": f"Biz {i}"} for i in range(10)]
            return httpx.Response(200, json={"results": items, "page": 0})
        elif page == "1":
            return httpx.Response(200, json=TINYFISH_RESPONSE_P1)
        return httpx.Response(200, json={"results": [], "page": int(page)})

    async with _client(handler) as client:
        out = await provider.fetch(client, {"tinyfish_api_key": "tf_key"},
                                   D.DiscoveryQuery(term="roofer", location="Denver", depth=20))

    assert pages_called == ["0", "1"]
    assert len(out) == 11
    assert out[-1].domain == "apexroofing.example"


@pytest.mark.asyncio
async def test_tinyfish_rate_limit_retry():
    provider = D.TinyFish()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json=TINYFISH_RESPONSE_P0)

    async with _client(handler) as client:
        out = await provider.fetch(client, {"tinyfish_api_key": "tf_key"},
                                   D.DiscoveryQuery(term="roofer", location="Denver", depth=10))
    assert calls == 2
    assert len(out) == 2


@pytest.mark.asyncio
async def test_tinyfish_error_handling():
    provider = D.TinyFish()

    # 401 Unauthorized
    async with _client(lambda r: httpx.Response(401, json={"error": "unauthorized"})) as client:
        with pytest.raises(RuntimeError, match="Invalid or missing API key"):
            await provider.fetch(client, {"tinyfish_api_key": "bad"}, D.DiscoveryQuery(term="roofer"))

    # 402 Daily allowance used
    async with _client(lambda r: httpx.Response(402, json={"error": {"code": "INSUFFICIENT_CREDITS", "message": "Daily free Search allowance used"}})) as client:
        with pytest.raises(RuntimeError, match="HTTP 402"):
            await provider.fetch(client, {"tinyfish_api_key": "tf"}, D.DiscoveryQuery(term="roofer"))

    # 429 Repeated failure
    async with _client(lambda r: httpx.Response(429, headers={"Retry-After": "0.01"})) as client:
        with pytest.raises(RuntimeError, match="rate limited"):
            await provider.fetch(client, {"tinyfish_api_key": "tf"}, D.DiscoveryQuery(term="roofer"))

    # Missing API key in env
    async with _client(lambda r: httpx.Response(200, json={})) as client:
        with pytest.raises(RuntimeError, match="needs TINYFISH_API_KEY in .env"):
            await provider.fetch(client, {}, D.DiscoveryQuery(term="roofer"))


@pytest.mark.asyncio
async def test_dataforseo_errors_surface_the_message_not_just_the_code():
    """A bare status code costs several wrong guesses; the body usually makes
    the problem obvious."""
    payload = {"status_code": 40501, "status_message": "Invalid Field: 'categories'"}
    provider = D.DataForSEOListings()
    with pytest.raises(RuntimeError, match="Invalid Field"):
        async with _client(lambda r: httpx.Response(200, json=payload)) as client:
            await provider.fetch(client, {"dataforseo_login": "u",
                                          "dataforseo_password": "p"},
                                 D.DiscoveryQuery(term="roofer"))


def test_dataforseo_sandbox_is_never_the_live_host():
    provider = D.DataForSEOListings()
    assert "sandbox" in provider.base_url({"dataforseo_sandbox": "1"})
    assert "sandbox" not in provider.base_url({})


# ── The provider menu the dashboard renders ──


def test_provider_menu_explains_cost_and_configuration():
    menu = {m["key"]: m for m in D.provider_menu({"serper_api_key": "k", "tinyfish_api_key": "tf"})}
    assert menu["osm"]["configured"] is True and not menu["osm"]["needs_key"]
    assert menu["serper"]["configured"] is True
    assert menu["tinyfish"]["configured"] is True
    assert menu["tinyfish"]["needs_key"] is True
    assert menu["tinyfish"]["cost_note"] == "free"
    assert "30 searches a minute" in menu["tinyfish"]["free_tier"]
    assert "500 an hour" in menu["tinyfish"]["free_tier"]
    assert menu["dataforseo_listings"]["configured"] is False
    assert menu["dataforseo_listings"]["env_keys"] == [
        "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"]
    for entry in menu.values():
        assert entry["blurb"] and entry["cost_note"] and entry["signup_url"]


def test_the_default_provider_needs_no_account():
    """Someone should be able to run the whole pipeline before paying anyone."""
    default = D.PROVIDERS[D.DEFAULT_PROVIDER]
    assert default.env_keys == ()
    assert default.caveat, "a free source with thin coverage must say so"


@pytest.mark.asyncio
async def test_unconfigured_provider_says_which_key_is_missing(state, config, monkeypatch):
    from harvey.config import EnvConfig
    monkeypatch.setattr("harvey.config.load_env", lambda: EnvConfig())
    report = await D.run_discovery(state, config, "dataforseo_listings",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.stopped == "not configured"
    assert "DATAFORSEO_LOGIN" in report.errors[0]

    tf_report = await D.run_discovery(state, config, "tinyfish",
                                      [D.DiscoveryQuery(term="roofer")])
    assert tf_report.stopped == "not configured"
    assert "TINYFISH_API_KEY" in tf_report.errors[0]


@pytest.mark.asyncio
async def test_empty_api_key_runs_nothing_and_says_so(state, config,
                                                      register, monkeypatch):
    """The 2026-10-24 bug: an empty SERPER_API_KEY placeholder "succeeded"
    with 0 observations and exit code 0. It must stop with a named error."""
    monkeypatch.setattr("harvey.config.load_env",
                        lambda: EnvConfig(serper_api_key=""))
    fake = register(D.Serper())   # fresh instance — don't mutate the singleton

    async def must_not_fetch(client, env, query):
        raise AssertionError("an unconfigured provider must not be called")
    fake.fetch = must_not_fetch

    report = await D.run_discovery(state, config, "serper",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.stopped == "not configured"
    assert report.queries_ran == 0
    assert "SERPER_API_KEY" in report.errors[0]


@pytest.mark.asyncio
async def test_template_placeholder_key_is_treated_as_missing(state, config,
                                                              register,
                                                              monkeypatch):
    monkeypatch.setattr("harvey.config.load_env",
                        lambda: EnvConfig(serper_api_key="your_api_key_here"))
    fake = register(D.Serper())   # fresh instance — don't mutate the singleton

    async def must_not_fetch(client, env, query):
        raise AssertionError("a placeholder key must not be sent to the API")
    fake.fetch = must_not_fetch

    report = await D.run_discovery(state, config, "serper",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.stopped == "not configured"
    assert report.queries_ran == 0


@pytest.mark.parametrize("value,missing", [
    ("", True), ("   ", True), ("your_api_key_here", True),
    ("PLACEHOLDER", True), ("<serper-key>", True), ("xxxxxx", True),
    ("change-me", True), ("example_key", True),
    ("a1b2c3d4e5f67890abcdef", False),   # a live-looking key
    ("you@example.com", False),
    ("a1todo9bxxxxyour_key_z", False),
    ("placeholder-team@example.com", False),           # DataForSEO logins are emails
])
def test_missing_keys_flags_template_placeholders_not_real_keys(value, missing):
    assert bool(D.PROVIDERS["serper"].missing_keys(
        {"serper_api_key": value})) is missing


@pytest.mark.asyncio
async def test_queries_ran_reports_what_actually_executed(state, config,
                                                          register):
    fake = register(FakeProvider([[biz(domain="a.com")],
                                  [biz(domain="b.com")]]))
    report = await D.run_discovery(state, config, "fake", [
        D.DiscoveryQuery(term="roofer", location="Denver"),
        D.DiscoveryQuery(term="roofer", location="Aurora"),
    ])
    assert report.queries == 2
    assert report.queries_ran == 2
    assert report.as_dict()["queries_ran"] == 2


@pytest.mark.asyncio
async def test_queries_ran_is_zero_when_stopped_before_any_fetch(state, config,
                                                                 register):
    await state.set_setting("discovery_paused", "stopped by hand")
    fake = register(FakeProvider([[biz(domain="a.com")]]))
    report = await D.run_discovery(state, config, "fake",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.stopped == "paused"
    assert report.queries_ran == 0
    assert report.as_dict()["queries_ran"] == 0
    assert fake.calls == 0


def test_unknown_provider_fails_loudly():
    with pytest.raises(ValueError, match="unknown discovery provider"):
        import asyncio
        asyncio.run(D.run_discovery(None, None, "nope"))


@pytest.mark.asyncio
async def test_overpass_throttling_is_an_error_not_an_empty_market(monkeypatch):
    """A silent [] on a 504 is indistinguishable from 'no businesses here',
    which is how a throttled run gets mistaken for a clean one."""
    monkeypatch.setattr(D.OpenStreetMap, "BACKOFF", 0.0)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(504, text="")

    provider = D.OpenStreetMap()
    with pytest.raises(RuntimeError, match="throttling"):
        async with _client(handler) as client:
            await provider.fetch(client, {}, D.DiscoveryQuery(
                term="roofer", coordinate="39.7,-104.9,40"))
    assert seen == list(D.OpenStreetMap.ENDPOINTS), "every mirror should be tried"


@pytest.mark.asyncio
async def test_overpass_falls_over_to_the_next_mirror(monkeypatch):
    monkeypatch.setattr(D.OpenStreetMap, "BACKOFF", 0.0)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(504, text="")
        return httpx.Response(200, json={"elements": [{
            "type": "node", "id": 42,
            "tags": {"name": "Sandpiper Exteriors", "craft": "roofer",
                     "phone": "+15550100288", "addr:city": "Denver"},
        }]})

    provider = D.OpenStreetMap()
    async with _client(handler) as client:
        out = await provider.fetch(client, {}, D.DiscoveryQuery(
            term="roofer", coordinate="39.7,-104.9,40"))
    assert len(out) == 1
    assert out[0].name == "Sandpiper Exteriors"
    assert out[0].external_id == "osm:node/42"
    assert out[0].has_website is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_a_mirror_failing_transiently_hands_over_to_the_next(status, monkeypatch):
    """502 is the one that bit us: only 429 and 504 used to count as "try the
    next one", so a bad gateway on the first mirror sank a search the second
    mirror could have answered."""
    monkeypatch.setattr(D.OpenStreetMap, "BACKOFF", 0.0)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(status, text="")
        return httpx.Response(200, json={"elements": [{
            "type": "node", "id": 7,
            "tags": {"name": "Kestrel Exteriors", "craft": "roofer"},
        }]})

    provider = D.OpenStreetMap()
    async with _client(handler) as client:
        out = await provider.fetch(client, {}, D.DiscoveryQuery(
            term="roofer", coordinate="39.7,-104.9,40"))
    assert [b.name for b in out] == ["Kestrel Exteriors"]
    assert seen == list(D.OpenStreetMap.ENDPOINTS[:2]), "the first mirror's failure should not have been fatal"


@pytest.mark.asyncio
async def test_every_mirror_failing_is_an_error_that_names_the_cause(monkeypatch):
    """Throttling is not the only way the free mirrors go away, and the error
    a user reads has to be true of what actually happened."""
    monkeypatch.setattr(D.OpenStreetMap, "BACKOFF", 0.0)
    statuses = iter((502, 503, 500))
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(next(statuses), text="")

    provider = D.OpenStreetMap()
    with pytest.raises(RuntimeError, match="unavailable or throttling"):
        async with _client(handler) as client:
            await provider.fetch(client, {}, D.DiscoveryQuery(
                term="roofer", coordinate="39.7,-104.9,40"))
    assert seen == list(D.OpenStreetMap.ENDPOINTS), "every mirror should be tried"


@pytest.mark.asyncio
async def test_a_query_the_mirrors_reject_fails_at_the_first_one(monkeypatch):
    """A 400 means our query is wrong, not that the mirror is having a bad
    day. Asking two more volunteer servers the same bad question wastes their
    bandwidth and buries the real error under a retry story."""
    monkeypatch.setattr(D.OpenStreetMap, "BACKOFF", 0.0)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(400, text="malformed query")

    provider = D.OpenStreetMap()
    with pytest.raises(httpx.HTTPStatusError):
        async with _client(handler) as client:
            await provider.fetch(client, {}, D.DiscoveryQuery(
                term="roofer", coordinate="39.7,-104.9,40"))
    assert seen == list(D.OpenStreetMap.ENDPOINTS[:1]), "a rejected query is not a mirror problem"


def test_the_mirrors_are_separate_instances():
    """overpass.private.coffee resolved to the same address as
    overpass.kumi.systems, so the list held three URLs and two servers: a 502
    on the first mirror bought two tries at one machine. DNS cannot be checked
    offline, so this pins the pair we measured and the shape of the list;
    scripts/diagnose_overpass.py re-checks the addresses for real."""
    hosts = [u.split("/")[2] for u in D.OpenStreetMap.ENDPOINTS]
    assert len(set(hosts)) == len(hosts), "the same host twice is not a mirror"
    aliases = {"overpass.kumi.systems", "overpass.private.coffee"}
    assert len(aliases & set(hosts)) <= 1, "those two are one instance"


@pytest.mark.asyncio
async def test_overpass_asks_for_a_budget_a_queued_mirror_can_meet(monkeypatch):
    """A one-node query measured 38s of queueing on a loaded mirror, which the
    module's 45s read budget cannot absorb once the query is a real one."""
    monkeypatch.setattr(D.OpenStreetMap, "BACKOFF", 0.0)
    seen = {}

    def handler(request):
        seen["read"] = (request.extensions.get("timeout") or {}).get("read")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"elements": []})

    provider = D.OpenStreetMap()
    async with _client(handler) as client:
        await provider.fetch(client, {}, D.DiscoveryQuery(
            term="roofer", coordinate="39.7,-104.9,40"))
    assert seen["read"] == D.OpenStreetMap.HTTP_TIMEOUT.read > D.TIMEOUT.read
    assert f"[timeout:{D.OpenStreetMap.SERVER_TIMEOUT}]" in seen["body"],         "the server has to be told how long it may take, too"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout,
                                   httpx.ReadTimeout])
async def test_a_mirror_that_never_answers_hands_over_to_the_next(error, monkeypatch):
    """A mirror that accepts the connection and then goes quiet is the failure
    we actually hit; it never had a test, only the ones that answer."""
    monkeypatch.setattr(D.OpenStreetMap, "BACKOFF", 0.0)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            raise error("", request=request)
        return httpx.Response(200, json={"elements": [{
            "type": "node", "id": 9,
            "tags": {"name": "Pelham Roofing", "craft": "roofer"},
        }]})

    provider = D.OpenStreetMap()
    async with _client(handler) as client:
        out = await provider.fetch(client, {}, D.DiscoveryQuery(
            term="roofer", coordinate="39.7,-104.9,40"))
    assert [b.name for b in out] == ["Pelham Roofing"]
    assert seen == list(D.OpenStreetMap.ENDPOINTS[:2])


@pytest.mark.asyncio
async def test_mirrors_that_all_hang_raise_instead_of_an_empty_market(monkeypatch):
    """Same rule as a throttle: silence from every mirror must not read as a
    market with no businesses in it."""
    monkeypatch.setattr(D.OpenStreetMap, "BACKOFF", 0.0)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        raise httpx.ReadTimeout("", request=request)

    provider = D.OpenStreetMap()
    with pytest.raises(RuntimeError):
        async with _client(handler) as client:
            await provider.fetch(client, {}, D.DiscoveryQuery(
                term="roofer", coordinate="39.7,-104.9,40"))
    assert seen == list(D.OpenStreetMap.ENDPOINTS), "every mirror should be tried"


@pytest.mark.asyncio
async def test_overpass_needs_a_coordinate_and_says_so(caplog):
    provider = D.OpenStreetMap()
    async with _client(lambda r: httpx.Response(200, json={})) as client:
        out = await provider.fetch(client, {}, D.DiscoveryQuery(
            term="roofer", location="Denver, CO"))
    assert out == []
    assert "coordinate" in caplog.text.lower()


# ── Geocoding ──


def _geo_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_geocode_is_cached_so_a_city_is_looked_up_once(state):
    """Nominatim permits single place lookups and forbids bulk enumeration.
    Caching forever is what keeps us on the right side of that."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        assert "Harvey" in request.headers["user-agent"]
        return httpx.Response(200, json=[{"lat": "39.7392", "lon": "-104.9903"}])

    async with _geo_client(handler) as client:
        assert await REAL_GEOCODE(state, "Denver, CO", client=client) \
            == "39.7392,-104.9903,40"
        assert await REAL_GEOCODE(state, "Denver, CO", client=client) \
            == "39.7392,-104.9903,40"
        assert await REAL_GEOCODE(state, "DENVER, co", client=client) \
            == "39.7392,-104.9903,40"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_geocode_miss_is_cached_too(state):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=[])

    async with _geo_client(handler) as client:
        assert await REAL_GEOCODE(state, "Nowhere XYZ", client=client) == ""
        assert await REAL_GEOCODE(state, "Nowhere XYZ", client=client) == ""
    assert calls["n"] == 1, "a miss must not be re-asked on every run"


@pytest.mark.asyncio
async def test_configured_coordinates_win_over_geocoding(state, monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("should not geocode a query that already has a point")
    monkeypatch.setattr(D, "geocode_city", boom)

    queries = [D.DiscoveryQuery(term="roofer", location="Denver, CO",
                                coordinate="1.0,2.0,25")]
    out = await REAL_RESOLVE(state, queries)
    assert out[0].coordinate == "1.0,2.0,25"


@pytest.mark.asyncio
async def test_one_geocode_per_city_not_per_query(state, monkeypatch):
    """Twelve queries across four cities is four lookups, not twelve."""
    monkeypatch.setattr(D, "geocode_city", REAL_GEOCODE)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=[{"lat": "1.0", "lon": "2.0"}])

    queries = [D.DiscoveryQuery(term=t, location=c)
               for t in ("roofing", "gutters", "siding")
               for c in ("Denver, CO", "Dallas, TX", "Tampa, FL", "Nashville, TN")]
    async with _geo_client(handler) as client:
        await REAL_RESOLVE(state, queries, client=client)
    assert calls["n"] == 4
    assert all(q.coordinate == "1.0000,2.0000,40" for q in queries)


@pytest.mark.asyncio
async def test_osm_refuses_a_term_it_cannot_map_instead_of_guessing():
    """An earlier fallback to `office=company` answered "roofers in Denver"
    with every registered office in Denver. Returning nothing is correct;
    returning plausible junk is not."""
    provider = D.OpenStreetMap()
    with pytest.raises(RuntimeError, match="no category"):
        async with _client(lambda r: httpx.Response(200, json={})) as client:
            await provider.fetch(client, {}, D.DiscoveryQuery(
                term="storm damage restoration", coordinate="39.7,-104.9,40"))


@pytest.mark.asyncio
async def test_osm_maps_the_trades_it_does_know():
    provider = D.OpenStreetMap()
    assert provider._tags("Roofing contractors") == ["craft=roofer", "shop=roofing"]
    assert provider._tags("Residential roofing") == ["craft=roofer", "shop=roofing"]
    assert provider._tags("Emergency plumber") == ["craft=plumber"]
    assert provider._tags("cosmetic dentist") == ["amenity=dentist"]
    assert provider._tags("B2B SaaS") == []


@pytest.mark.asyncio
async def test_an_unmappable_term_is_reported_not_silently_skipped(
        state, config, register, monkeypatch):
    """The run must surface why a query produced nothing."""
    monkeypatch.setitem(D.PROVIDERS, "osm", D.OpenStreetMap())

    async def fake_fetch(self, client, env, query):
        raise RuntimeError("OpenStreetMap has no category for 'widgets'")
    monkeypatch.setattr(D.OpenStreetMap, "fetch", fake_fetch)

    report = await D.run_discovery(state, config, "osm",
                                   [D.DiscoveryQuery(term="widgets",
                                                     coordinate="1,2,40")])
    assert report.found == 0
    assert report.errors and "no category" in report.errors[0]


# ── business names: SERP titles are page titles, not names ──

@pytest.mark.parametrize("title,domain,expected", [
    ("Northvale Property Management: HOA Management Company in ...", "northvaleproperty.com",
     "Northvale Property Management"),
    ("Home Page - NVCM | Community Association Management", "nvcmnet.com", "NVCM"),
    ("Northvale Association of Community Managers", "nacm.org",
     "Northvale Association of Community Managers"),
    ("Brightwater Texas: Texas HOA Property Management Company", "brightwatertexas.com",
     "Brightwater Texas"),
    # No segment names the business → fall back to the domain, split sensibly.
    ("Denver, CO | HOA Management Firm", "managementharbor.com", "Management Harbor"),
    ("Texas HOA management", "kqresidential.com", "KQ Residential"),
    ("Great Texas Community Management Begins Here", "growwithus.us", "Grow With Us"),
])
def test_business_name_from_serp_title(title, domain, expected):
    assert D.business_name(title, domain) == expected


def test_generic_words_alone_never_match_the_domain():
    """'Management' is in managementharbor.com but says nothing about it."""
    assert not D.name_matches_domain("HOA Management Firm", "managementharbor.com")
    assert D.name_matches_domain("The Management Harbor", "managementharbor.com")


def test_site_name_prefers_og_then_title_and_needs_domain_backing():
    og = '<meta property="og:site_name" content="Grow Community Management" />'
    assert D.site_name_from_html(og, "growwithus.us") == "Grow Community Management"
    title = "<title>Lumen Association Management - HOA Company in TX &amp; CO</title>"
    assert D.site_name_from_html(title, "lumenam.com") == "Lumen Association Management"
    both = ('<meta property="og:site_name" content="Management Harbor">'
            "<title>The Management Harbor: Community Management</title>")
    assert D.site_name_from_html(both, "managementharbor.com") == "The Management Harbor"
    assert D.site_name_from_html("<title>Attention Required! | Cloudflare</title>",
                                 "lumenam.com") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['serper', 'dataforseo_serp', 'scavio_search'])
async def test_directory_title_still_filtered_after_adapter(provider, tmp_path, monkeypatch):
    title = 'Top 10 Roofing Companies in Denver'
    domain = 'milehighguide.com'
    assert D.is_junk(domain, title)
    item = {'title': title, 'link': f'https://{domain}/roofers',
            'url': f'https://{domain}/roofers', 'domain': domain,
            'position': 1, 'rank_group': 1, 'type': 'organic'}
    valid = dict(item, title='Kettleman Roofing | Denver',
                 domain='kettlemanroofing.com', link='https://kettlemanroofing.com',
                 url='https://kettlemanroofing.com', position=2, rank_group=2)
    payload = ({'organic': [item, valid]} if provider == 'serper' else
               {'organic_results': [item, valid]} if provider == 'scavio_search' else
               {'status_code': 20000, 'tasks': [{'status_code': 20000,
                 'result': [{'items': [item, valid]}]}]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(D.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), **kw))
    monkeypatch.setattr('harvey.config.load_env', lambda: EnvConfig(
        serper_api_key='dummy', dataforseo_login='dummy', dataforseo_password='dummy',
        scavio_api_key='dummy'))
    state = StateManager(str(tmp_path / 'test.db'))
    await state.init_db()
    await seed_signal_catalog(state)
    for signal in SIGNAL_CATALOG:
        await state.set_signal_status(signal['code'], 'confirmed')
    report = await D.run_discovery(state, load_config(), provider_key=provider,
        queries=[D.DiscoveryQuery(term='roofer', location='Denver')])
    assert report.errors == []
    assert report.junk == 1, report.as_dict()
    assert report.found == 2
    assert report.new_companies == 1
    import sqlite3
    with closing(sqlite3.connect(state.db_path)) as db:
        assert db.execute("SELECT domain, name FROM companies").fetchall() == [
            ("kettlemanroofing.com", "Kettleman Roofing")]


@pytest.mark.parametrize('title,domain,expected', [
    ('Contact Us | ContactCraft', 'contactcraft.com', 'ContactCraft'),
    ('Craft | ContactCraft', 'contactcraft.com', 'ContactCraft'),
    ('Roofing Services in Denver | Kettleman Roofing', 'kettlemanroofing.com', 'Kettleman Roofing'),
    ('Plumbing Services in Austin | Acme Plumbing', 'acmeplumbing.com', 'Acme Plumbing'),
])
def test_brand_wins_over_generic_service_segment(title, domain, expected):
    assert D.business_name(title, domain) == expected


@pytest.mark.parametrize("service,brand,domain", [
    ("Landscaping", "Kettleman Landscaping", "kettlemanlandscaping.com"),
    ("Electrical", "Kettleman Electrical", "kettlemanelectrical.com"),
])
def test_service_location_labels_do_not_establish_brand_identity(service, brand, domain):
    label = f"{service} Services in Denver"
    assert not D.name_matches_domain(label, domain)
    assert D.business_name(f"{label} | {brand}", domain) == brand
    html = f'<meta property="og:site_name" content="{brand}"><title>{label}</title>'
    assert D.site_name_from_html(html, domain) == brand
    assert D.site_name_from_html(f"<title>{label}</title>", domain) == ""


def test_homepage_prefers_stronger_brand_over_longer_partial_match():
    html = ('<meta property="og:site_name" content="Kettleman Landscaping">'
            '<title>Landscaping Professionals</title>')
    assert D.site_name_from_html(html, "kettlemanlandscaping.com") == "Kettleman Landscaping"


# --- CLI startup key guard -------------------------------------------
#
# `harvey discover --provider serper` with an empty or placeholder
# SERPER_API_KEY used to exit 0 and record zero observations. cmd_discover
# must refuse at startup - before the estimate, before any call - in both
# collect mode, and never block estimates or the keyless default provider.

from types import SimpleNamespace

from harvey.cli import cmd_discover


def _cli_args(**over):
    base = dict(provider="serper", providers=False, estimate=False,
                city="", depth=30, limit=100, max_spend=1.0, no_profile=True)
    base.update(over)
    return SimpleNamespace(**base)


def _fake_env(monkeypatch, **values):
    from harvey import config as cfg
    monkeypatch.setattr(
        cfg, "load_env",
        lambda: SimpleNamespace(model_dump=lambda: dict(values)))


def test_missing_key_fails_loudly_at_startup(monkeypatch, capsys):
    _fake_env(monkeypatch, serper_api_key="")
    with pytest.raises(SystemExit) as ei:
        cmd_discover(_cli_args())
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "provider skipped: missing key" in out
    assert "SERPER_API_KEY" in out
    assert "0 queries can run" in out
    # The guard fires before the estimate is even attempted.
    assert "Estimated cost" not in out


def test_placeholder_key_is_treated_as_missing(monkeypatch, capsys):
    _fake_env(monkeypatch, serper_api_key="your_key_here")
    with pytest.raises(SystemExit) as ei:
        cmd_discover(_cli_args())
    assert ei.value.code == 1
    assert "provider skipped: missing key" in capsys.readouterr().out


def test_absent_key_is_treated_as_missing(monkeypatch, capsys):
    _fake_env(monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cmd_discover(_cli_args())
    assert ei.value.code == 1
    assert "SERPER_API_KEY" in capsys.readouterr().out


def test_keyless_default_provider_is_never_blocked(monkeypatch, capsys):
    # osm has no env_keys, so the startup guard must pass through. Reaching
    # load_config (patched to raise) proves the guard let the command by.
    _fake_env(monkeypatch)
    from harvey import config as cfg

    def _boom():
        raise RuntimeError("reached-load-config")

    monkeypatch.setattr(cfg, "load_config", _boom)
    with pytest.raises(RuntimeError, match="reached-load-config"):
        cmd_discover(_cli_args(provider="osm"))


def test_tinyfish_measures_no_rank_signal():
    """A TinyFish run must not mark businesses Serper ranked as 'gone'."""
    assert D.TinyFish().rank_codes == ()


@pytest.mark.asyncio
async def test_tinyfish_paces_itself_under_30_a_minute(monkeypatch):
    provider = D.TinyFish()
    waits = []

    async def _sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(D.asyncio, "sleep", _sleep)
    items = [{"url": f"https://biz{i}.example", "title": f"Biz {i}"} for i in range(10)]
    async with _client(lambda r: httpx.Response(200, json={"results": items})) as client:
        await provider.fetch(client, {"tinyfish_api_key": "tf"},
                             D.DiscoveryQuery(term="roofer", location="Denver", depth=30))
        await provider.fetch(client, {"tinyfish_api_key": "tf"},
                             D.DiscoveryQuery(term="plumber", location="Denver", depth=10))
    # four requests, so three waits, each long enough for 30 a minute
    assert len([w for w in waits if w >= D.TinyFish.MIN_INTERVAL - 0.1]) >= 3


@pytest.mark.asyncio
async def test_tinyfish_rate_limit_names_the_real_limits(monkeypatch):
    async def _sleep(seconds):
        return None
    monkeypatch.setattr(D.asyncio, "sleep", _sleep)
    async with _client(lambda r: httpx.Response(429, headers={"Retry-After": "1"})) as client:
        with pytest.raises(RuntimeError) as exc:
            await D.TinyFish().fetch(client, {"tinyfish_api_key": "tf"},
                                     D.DiscoveryQuery(term="roofer"))
    assert "30" in str(exc.value) and "500 an hour" in str(exc.value)


def test_tinyfish_docs_state_the_published_limits():
    from harvey.paths import PROJECT_ROOT
    for name in ("README.md", "CLAUDE.md", ".env.example", "CHANGELOG.md"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        for claim in ("12,000 free", "12,000 queries", "12,000 searches", "12k free"):
            assert claim not in text, (name, claim)


# ── Review round: a provider out of allowance stops the run ──

class _Exhausted(FakeProvider):
    key = "fake"

    async def fetch(self, client, env, query):
        self.calls += 1
        raise D.ProviderExhausted("TinyFish's 500 searches an hour are used up")


@pytest.mark.asyncio
async def test_a_provider_out_of_allowance_stops_the_run(state, config, register):
    provider = register(_Exhausted([]))
    q = [D.DiscoveryQuery(term="roofer", location=c) for c in ("a", "b", "c")]
    report = await D.run_discovery(state, config, "fake", q, max_spend=5.0)
    assert provider.calls == 1
    assert "500 searches an hour" in report.stopped


@pytest.mark.asyncio
async def test_tinyfish_stops_before_the_hourly_allowance_is_spent():
    provider = D.TinyFish()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": [{"url": f"https://b{calls}.example",
                                                      "title": "B"}]})
    async with _client(handler) as client:
        with pytest.raises(D.ProviderExhausted):
            for _ in range(D.TinyFish.HOURLY_LIMIT + 5):
                await provider.fetch(client, {"tinyfish_api_key": "tf"},
                                     D.DiscoveryQuery(term="roofer", depth=10))
    assert calls <= D.TinyFish.HOURLY_LIMIT


@pytest.mark.asyncio
async def test_tinyfish_rate_limited_twice_stops_the_run_not_the_phrase():
    async with _client(lambda r: httpx.Response(429, headers={"Retry-After": "1"})) as client:
        with pytest.raises(D.ProviderExhausted):
            await D.TinyFish().fetch(client, {"tinyfish_api_key": "tf"},
                                     D.DiscoveryQuery(term="roofer"))


@pytest.mark.asyncio
async def test_tinyfish_skips_a_malformed_item_and_keeps_the_rest():
    page0 = {"results": [{"url": f"https://b{i}.example", "title": "B"} for i in range(10)]}
    page1 = {"results": [None, "junk", {"title": "no url", "site_name": "Example Roofing"},
                         {"url": "https://kept.example", "title": "Kept"}]}

    def handler(request):
        return httpx.Response(200, json=page0 if request.url.params.get("page") == "0" else page1)
    async with _client(handler) as client:
        out = await D.TinyFish().fetch(client, {"tinyfish_api_key": "tf"},
                                       D.DiscoveryQuery(term="roofer", depth=20))
    domains = [b.domain for b in out]
    assert len(domains) == 11 and domains[-1] == "kept.example"


@pytest.mark.asyncio
async def test_tinyfish_a_non_object_body_is_an_error_not_a_crash():
    async with _client(lambda r: httpx.Response(200, json=["not", "an", "object"])) as client:
        with pytest.raises(RuntimeError, match="unexpected"):
            await D.TinyFish().fetch(client, {"tinyfish_api_key": "tf"},
                                     D.DiscoveryQuery(term="roofer"))


def test_tinyfish_pacing_works_across_event_loops():
    """The registry's one instance is used by runs in different loops."""
    provider = D.TinyFish()
    ok = httpx.Response(200, json={"results": []})

    async def one():
        async with _client(lambda r: ok) as client:
            await provider.fetch(client, {"tinyfish_api_key": "tf"},
                                 D.DiscoveryQuery(term="roofer"))
    asyncio.run(one())
    asyncio.run(one())


# ── The do-not-contact list ──


@pytest.mark.asyncio
async def test_a_forbidden_company_never_becomes_a_row(state, config, register):
    """Applied before the upsert, so a forbidden company costs nothing to
    enrich and never turns up in a cohort.

    Counted apart from `junk` deliberately: junk asks whether a result is a
    business at all, this asks whether we are allowed to approach one.
    """
    # Clear the source path FIRST: live_do_not_contact re-reads the config file
    # when there is one and writes the file's value back over the field, so
    # setting the field before clearing the path would lose it.
    config._source_path = None
    config.compliance.do_not_contact = ["rival.example", "jane@other.example"]
    register(FakeProvider([[
        biz(domain="rival.example"),         # listed by domain
        biz(domain="shop.rival.example"),    # subdomain of a listed domain
        biz(domain="other.example"),         # only an ADDRESS there is listed
        biz(domain="notrival.example"),      # lookalike, a different company
        biz(domain="real.example"),
    ]]))
    report = await D.run_discovery(state, config, "fake",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.excluded == 2
    assert report.junk == 0
    assert report.new_companies == 3
    assert not await state.company_exists("rival.example")
    assert not await state.company_exists("shop.rival.example")
    # An address entry bars a mailbox, not the company: still discoverable.
    assert await state.company_exists("other.example")
    assert await state.company_exists("notrival.example")


@pytest.mark.asyncio
async def test_an_empty_list_changes_discovery_not_at_all(state, config, register):
    """The positive control. Without it, the test above could pass because
    nothing was admitted rather than because the right two were refused."""
    config._source_path = None
    config.compliance.do_not_contact = []
    register(FakeProvider([[biz(domain="rival.example"), biz(domain="real.example")]]))
    report = await D.run_discovery(state, config, "fake",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.excluded == 0
    assert report.new_companies == 2
    assert await state.company_exists("rival.example")


@pytest.mark.parametrize("domain", ["jobs.example.com", "careers.example.org", "example.test"])
def test_new_junk_filters_keep_employer_sites(domain):
    assert not D.is_junk(domain)


@pytest.mark.parametrize("domain", ["naukri.com", "www.indiamart.com", "thingiverse.com"])
def test_new_junk_filters_reject_aggregators(domain):
    from harvey.agents.scout import Scout
    assert D.is_junk(domain)
    assert Scout._is_noise_domain(None, domain)


@pytest.mark.asyncio
async def test_serper_caps_request_and_estimate():
    import json
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"organic": []})
    query = D.DiscoveryQuery(term="roofer", depth=100)
    provider = D.Serper()
    async with _client(respond) as client:
        await provider.fetch(client, {"serper_api_key": "fixture"}, query)
    assert requests[0]["num"] == 10
    assert provider.estimate([query]) == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_scout_serper_caps_request(monkeypatch):
    from harvey.agents.scout import Scout
    from types import SimpleNamespace
    requests = []
    async def fetch(url, **kwargs):
        requests.append(kwargs["json_body"])
        return httpx.Response(200, json={"organic": []})
    await Scout._search_serper(SimpleNamespace(_fetch=fetch), "roofer", "fixture")
    assert requests == [{"q": "roofer", "num": 10}]


@pytest.mark.asyncio
async def test_failed_attempt_is_not_a_successful_query(state, config, register):
    fake = register(FakeProvider([]))
    async def fail(*args):
        raise RuntimeError("fixture failure")
    fake.fetch = fail
    report = await D.run_discovery(state, config, "fake", [D.DiscoveryQuery(term="roofer")])
    assert report.queries_ran == 1
    assert report.queries_succeeded == 0
    assert report.errors


@pytest.mark.parametrize("attempted,succeeded", [(0, 0), (2, 0), (2, 1), (1, 1)])
def test_cli_exit_tracks_successful_queries(monkeypatch, config, attempted, succeeded):
    from harvey import config as cfg, pipeline
    _fake_env(monkeypatch, serper_api_key="fixture")
    monkeypatch.setattr(cfg, "load_config", lambda: config)
    async def init_db(self):
        pass
    async def estimate(*args, **kwargs):
        return D.DiscoveryReport()
    async def prospect(*args, **kwargs):
        return SimpleNamespace(discover={"queries": 2, "queries_ran": attempted,
                                        "queries_succeeded": succeeded}, errors=[])
    monkeypatch.setattr(StateManager, "init_db", init_db)
    monkeypatch.setattr(D, "run_discovery", estimate)
    monkeypatch.setattr(pipeline, "run_prospecting", prospect)
    if succeeded:
        cmd_discover(_cli_args())
    else:
        with pytest.raises(SystemExit) as exc:
            cmd_discover(_cli_args())
        assert exc.value.code == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403, 429])
async def test_scavio_stops_run_on_account_errors(state, config, register, monkeypatch, status):
    provider = register(D.ScavioSearch())
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "fixture_secret_should_not_be_logged"})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(D.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("harvey.config.load_env", lambda: EnvConfig(scavio_api_key="fixture"))
    report = await D.run_discovery(state, config, provider.key,
        [D.DiscoveryQuery(term="roofer"), D.DiscoveryQuery(term="plumber")])
    assert len(calls) == 1
    assert str(status) in report.stopped
    assert "fixture_secret" not in str(report.as_dict())
    assert report.queries_succeeded == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [402, 502])
async def test_scavio_partial_search_preserves_prior_ranks_and_cost(state, config, register,
                                                                  monkeypatch, failure):
    provider = register(D.ScavioSearch())
    calls = []
    phase = ["seed"]
    def handler(request):
        start = json.loads(request.content)["start"]
        calls.append(start)
        if phase[0] == "partial" and start:
            return httpx.Response(failure, json={"error": "fixture"})
        return httpx.Response(200, json=_scavio_serp_page(start, 10, last=start == 10))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(D.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("harvey.config.load_env", lambda: EnvConfig(scavio_api_key="fixture"))
    query = D.DiscoveryQuery(term="roofer", depth=20)
    await D.run_discovery(state, config, provider.key, [query])
    previous = await state.get_company_by_domain("roofer10.example")
    phase[0] = "partial"
    report = await D.run_discovery(state, config, provider.key, [query])
    assert report.actual_cost == pytest.approx(0.01)
    assert report.found == 10 and report.errors
    assert bool(report.stopped) is (failure == 402)
    readings = [o for o in await state.get_observations()
                if o["company_id"] == previous.id and o["signal_code"] == "SERP_RANK"]
    assert readings and all(o["value_num"] == 11 for o in readings)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], {"organic_results": {}}, {"error": "fixture"},
                                  {"organic_results": [{"link": {}}]}])
async def test_scavio_malformed_paid_responses_are_failures_with_cost(body):
    provider = D.ScavioSearch()
    async with _client(lambda r: httpx.Response(200, json=body)) as client:
        with pytest.raises(RuntimeError, match="Scavio: invalid"):
            await provider.fetch(client, {"scavio_api_key": "fixture"}, D.DiscoveryQuery(term="roofer"))
    assert provider.last_cost == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_scavio_cost_resets_and_budget_stops_before_next_page():
    provider = D.ScavioSearch()
    provider.remaining_budget = 0.01
    starts = []
    def handler(request):
        start = json.loads(request.content)["start"]
        starts.append(start)
        return httpx.Response(200, json=_scavio_serp_page(start, 10))
    async with _client(handler) as client:
        for _ in range(2):
            rows = await provider.fetch(client, {"scavio_api_key": "fixture"},
                                        D.DiscoveryQuery(term="roofer", depth=30))
            assert len(rows) == 10
            assert provider.last_cost == pytest.approx(0.01)
            assert "budget reached" in provider.stop_reason
    assert starts == [0, 0]


@pytest.mark.asyncio
async def test_scavio_maps_resolves_coordinates_in_run(state, config, register, monkeypatch):
    provider = register(D.ScavioMaps())
    resolved = []
    async def resolve(state, queries):
        resolved.extend(queries)
        return queries
    monkeypatch.setattr(D, "resolve_coordinates", resolve)
    await D.run_discovery(state, config, provider.key, [D.DiscoveryQuery(term="roofer")], dry_run=True)
    assert len(resolved) == 1


@pytest.mark.parametrize("coordinate", ["nan,0,40", "0,inf,40", "91,0,40", "0,181,40", "bad,0,40"])
def test_scavio_invalid_map_centers_fall_back_to_place_name(coordinate):
    assert D.ScavioMaps.map_center(coordinate) == ""
