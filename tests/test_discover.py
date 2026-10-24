"""DISCOVER — junk filtering, entity resolution, cost discipline, adapters.

The expensive stage, so the tests care most about the things that cost money
or corrupt the database: that an estimate is honest, that a budget is
enforced before spending rather than after, that a re-run doesn't duplicate
anyone, and that directories never make it in.
"""

import asyncio
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
    menu = {m["key"]: m for m in D.provider_menu({"serper_api_key": "k"})}
    assert menu["osm"]["configured"] is True and not menu["osm"]["needs_key"]
    assert menu["serper"]["configured"] is True
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
async def test_unconfigured_provider_says_which_key_is_missing(state, config):
    report = await D.run_discovery(state, config, "dataforseo_listings",
                                   [D.DiscoveryQuery(term="roofer")])
    assert report.stopped == "not configured"
    assert "DATAFORSEO_LOGIN" in report.errors[0]


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
@pytest.mark.parametrize('provider', ['serper', 'dataforseo_serp'])
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
               {'status_code': 20000, 'tasks': [{'status_code': 20000,
                 'result': [{'items': [item, valid]}]}]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(D.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), **kw))
    monkeypatch.setattr('harvey.config.load_env', lambda: EnvConfig(
        serper_api_key='dummy', dataforseo_login='dummy', dataforseo_password='dummy'))
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
