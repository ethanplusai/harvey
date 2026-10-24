"""City rank as a discovery signal — DataForSEO organic + local pack + Maps.

Fixtures follow the response shapes documented at docs.dataforseo.com
(serp/google/organic/live/advanced, serp/google/maps/live/advanced). Nothing
here reaches the network: every HTTP call goes through httpx.MockTransport.
"""

import json
import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.config import EnvConfig, load_config
from harvey.models import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

ENV = {"dataforseo_login": "u", "dataforseo_password": "p"}
REAL_CLIENT = httpx.AsyncClient


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def no_geocode(state, city, radius_km=40, client=None):
        return ""

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(D, "geocode_city", no_geocode)
    monkeypatch.setattr(D.asyncio, "sleep", no_sleep)


def _client(handler):
    return REAL_CLIENT(transport=httpx.MockTransport(handler))


def _envelope(items, cost=0.006, task_status=20000, message="Ok."):
    return {"status_code": 20000, "cost": cost, "tasks": [{
        "status_code": task_status, "status_message": message,
        "result": [{"items": items}] if items is not None else None}]}


ORGANIC_WITH_PACK = _envelope([
    {"type": "local_pack", "rank_group": 1, "rank_absolute": 1, "position": "left",
     "title": "Sponsored Roofing", "domain": "adroofing.com", "is_paid": True,
     "cid": "111"},
    {"type": "local_pack", "rank_group": 2, "rank_absolute": 2, "position": "left",
     "title": "Kettleman Roofing", "domain": "kettlemanroofing.com",
     "url": "https://kettlemanroofing.com/", "phone": "+1 555-0100",
     "is_paid": False, "cid": "222",
     "rating": {"rating_type": "Max5", "value": 4.8, "votes_count": 210,
                "rating_max": 5}},
    {"type": "local_pack", "rank_group": 3, "rank_absolute": 3, "position": "left",
     "title": "Bob's Roof Fix", "domain": None, "url": None,
     "phone": "+1 555-0199", "is_paid": False, "cid": "333",
     "rating": {"rating_type": "Max5", "value": 4.1, "votes_count": 9,
                "rating_max": 5}},
    {"type": "organic", "rank_group": 1, "rank_absolute": 4, "position": "left",
     "domain": "northvaleroofing.com", "url": "https://northvaleroofing.com/",
     "title": "Northvale Roofing"},
    {"type": "organic", "rank_group": 14, "rank_absolute": 20, "position": "left",
     "domain": "kettlemanroofing.com", "url": "https://kettlemanroofing.com/denver",
     "title": "Kettleman Roofing | Denver"},
])

MAPS = _envelope([
    {"type": "maps_paid_item", "rank_group": 1, "rank_absolute": 1,
     "domain": "adroofing.com", "title": "Sponsored Roofing"},
    {"type": "maps_search", "rank_group": 1, "rank_absolute": 2,
     "domain": "kettlemanroofing.com", "title": "Kettleman Roofing",
     "url": "https://kettlemanroofing.com/", "phone": "+1 555-0100",
     "rating": {"rating_type": "Max5", "value": 4.8, "votes_count": 210,
                "rating_max": 5},
     "address": "1 Main St, Denver, CO 80202",
     "address_info": {"city": "Denver", "region": "CO", "country_code": "US"},
     "place_id": "ChIJkettle", "cid": "222", "feature_id": "0x1:0x2",
     "category": "Roofing contractor", "is_claimed": True},
    {"type": "maps_search", "rank_group": 7, "rank_absolute": 8,
     "domain": None, "title": "Bob's Roof Fix", "url": None,
     "phone": "+1 555-0199",
     "rating": {"rating_type": "Max5", "value": 4.1, "votes_count": 9,
                "rating_max": 5},
     "address_info": {"city": "Aurora", "region": "CO"},
     "place_id": "ChIJbob", "cid": "333", "category": "Roofer",
     "is_claimed": False},
], cost=0.002)


# ── The request: DataForSEO requires a location ──


async def _capture(provider, query, payload=ORGANIC_WITH_PACK):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)[0]
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        out = await provider.fetch(client, ENV, query)
    return seen, out


@pytest.mark.asyncio
async def test_organic_sends_the_geocoded_point_not_just_the_city_in_the_keyword():
    seen, _ = await _capture(D.DataForSEOSerp(), D.DiscoveryQuery(
        term="roofer", location="Denver, CO", coordinate="39.7392,-104.9903,40"))
    body = seen["body"]
    assert seen["url"].endswith("/serp/google/organic/live/advanced")
    # The configured radius is km for listings; the SERP point is the city
    # centre with a fixed, deliberate radius (see POINT_RADIUS).
    assert body["location_coordinate"] == "39.7392,-104.9903,2000"
    assert "location_name" not in body and "location_code" not in body
    assert body["keyword"] == "roofer Denver, CO"


@pytest.mark.asyncio
async def test_organic_falls_back_to_a_dataforseo_location_name():
    seen, _ = await _capture(D.DataForSEOSerp(), D.DiscoveryQuery(
        term="roofer", location="Denver, CO"))
    assert seen["body"]["location_name"] == "Denver,Colorado,United States"
    assert "location_coordinate" not in seen["body"]


@pytest.mark.parametrize("place,expected", [
    ("Denver, CO", "Denver,Colorado,United States"),
    ("Austin, Texas", "Austin,Texas,United States"),
    ("London, England, United Kingdom", "London,England,United Kingdom"),
    ("Toronto, Ontario, Canada", "Toronto,Ontario,Canada"),
])
def test_location_name_format(place, expected):
    assert D.dataforseo_location_name(place) == expected


@pytest.mark.asyncio
async def test_a_search_with_no_place_still_sends_a_location():
    """Non-local presets (YC) have no city; DataForSEO still requires one."""
    seen, _ = await _capture(D.DataForSEOSerp(), D.DiscoveryQuery(term="saas"))
    assert seen["body"]["location_code"] == 2840


@pytest.mark.asyncio
async def test_task_errors_raise_but_no_results_is_empty():
    bad = _envelope(None, task_status=40501, message="Invalid Field: 'location_name'.")
    with pytest.raises(RuntimeError, match="Invalid Field"):
        await _capture(D.DataForSEOSerp(), D.DiscoveryQuery(term="roofer"), bad)
    empty = _envelope(None, task_status=40102, message="No Search Results.")
    _, out = await _capture(D.DataForSEOSerp(), D.DiscoveryQuery(term="roofer"), empty)
    assert out == []


# ── Cost: Live mode, not the standard-queue price ──


def test_estimates_use_live_pricing():
    q30 = [D.DiscoveryQuery(term="roofer", location="Denver", depth=30)]
    # Live organic: $0.002 per page of 10 → depth 30 is three pages.
    assert D.estimate_cost("dataforseo_serp", q30) == pytest.approx(0.006)
    # Live Maps: $0.002 per SERP of up to 100 results.
    assert D.estimate_cost("dataforseo_maps", q30) == pytest.approx(0.002)
    q150 = [D.DiscoveryQuery(term="roofer", location="Denver", depth=150)]
    assert D.estimate_cost("dataforseo_maps", q150) == pytest.approx(0.004)


def test_rank_cost_notes_match_live_pricing():
    serp = next(s for s in SIGNAL_CATALOG if s["code"] == "SERP_RANK")
    assert "0.006" in serp["cost_note"] and "0.0006" not in serp["cost_note"]
    assert "0.006" in D.PROVIDERS["dataforseo_serp"].cost_note
    maps = next(s for s in SIGNAL_CATALOG if s["code"] == "MAPS_RANK")
    assert not maps["cost_note"].lower().startswith(("free", "included"))


# ── Local pack ──


@pytest.mark.asyncio
async def test_local_pack_is_parsed_and_ads_are_dropped():
    _, out = await _capture(D.DataForSEOSerp(), D.DiscoveryQuery(
        term="roofer", location="Denver, CO"))
    by_name = {b.name: b for b in out}
    assert "Sponsored Roofing" not in by_name, "is_paid pack entries are ads"

    kettleman = next(b for b in out if b.domain == "kettlemanroofing.com")
    # One business, both facts: pack slot 2 and organic 14.
    assert kettleman.local_pack_rank == 2 and kettleman.rank == 14
    assert kettleman.rating == 4.8 and kettleman.review_count == 210
    assert kettleman.keyword == "roofer Denver, CO"
    assert len([b for b in out if b.domain == "kettlemanroofing.com"]) == 1

    bob = by_name["Bob's Roof Fix"]
    assert bob.domain == "" and bob.external_id == "dataforseo:cid:333"
    assert bob.local_pack_rank == 3 and bob.rank is None
    # A pack entry without a website button is not proof of no website.
    assert bob.has_website is None

    northvale = next(b for b in out if b.domain == "northvaleroofing.com")
    assert northvale.rank == 1 and northvale.local_pack_rank is None


# ── Google Maps ──


@pytest.mark.asyncio
async def test_maps_adapter():
    provider = D.PROVIDERS["dataforseo_maps"]
    seen, out = await _capture(provider, D.DiscoveryQuery(
        term="roofer", location="Denver, CO", coordinate="39.7392,-104.9903,40"),
        payload=MAPS)
    assert seen["url"].endswith("/serp/google/maps/live/advanced")
    assert seen["body"]["location_coordinate"] == "39.7392,-104.9903,12z"
    assert seen["body"]["keyword"] == "roofer Denver, CO"
    assert provider.last_cost == pytest.approx(0.002)

    assert [b.name for b in out] == ["Kettleman Roofing", "Bob's Roof Fix"]
    kettleman, bob = out
    assert kettleman.maps_rank == 1 and kettleman.rank is None
    # Keyed on cid, the one id the local pack also carries; place_id is kept
    # as an alias so rows stored by it still resolve.
    assert kettleman.external_id == "dataforseo:cid:222"
    assert "dataforseo:ChIJkettle" in kettleman.alt_ids
    assert kettleman.category == "Roofing contractor"
    assert kettleman.is_claimed is True and kettleman.has_website is True

    assert bob.maps_rank == 7 and bob.domain == ""
    assert bob.identity() == "dataforseo:cid:333"
    assert bob.has_website is False and bob.is_claimed is False
    assert bob.location == "Aurora, CO"


@pytest.mark.asyncio
async def test_maps_without_coordinates_uses_location_name():
    seen, _ = await _capture(D.PROVIDERS["dataforseo_maps"], D.DiscoveryQuery(
        term="roofer", location="Denver, CO"), payload=MAPS)
    assert seen["body"]["location_name"] == "Denver,Colorado,United States"


@pytest.mark.asyncio
async def test_rank_providers_are_geocoded_before_the_run(state, monkeypatch):
    calls = []

    async def geocode(state, city, radius_km=40, client=None):
        calls.append(city)
        return "39.7392,-104.9903,40"

    monkeypatch.setattr(D, "geocode_city", geocode)
    for key in ("dataforseo_serp", "dataforseo_maps"):
        q = [D.DiscoveryQuery(term="roofer", location="Denver, CO")]
        await D.run_discovery(state, load_config(), key, q, dry_run=True)
        assert q[0].coordinate == "39.7392,-104.9903,40"
    assert calls == ["Denver, CO", "Denver, CO"]


# ── Keyword variants ──


def test_keyword_variants_multiply_terms_by_cities():
    config = load_config()
    config.icp.industries = ["roofing contractor"]
    config.icp.geography = ["Denver, CO", "Boulder, CO"]
    config.icp.keyword_variants = {"roofing contractor": ["roofer", "roof repair"]}
    queries = D.build_queries(config)
    assert sorted(q.keyword() for q in queries) == sorted([
        "roofing contractor Denver, CO", "roofer Denver, CO", "roof repair Denver, CO",
        "roofing contractor Boulder, CO", "roofer Boulder, CO", "roof repair Boulder, CO",
    ])


def test_without_variants_each_industry_is_its_own_search():
    config = load_config()
    config.icp.industries = ["roofing contractor"]
    config.icp.geography = ["Denver, CO"]
    config.icp.keyword_variants = {}
    assert [q.keyword() for q in D.build_queries(config)] == [
        "roofing contractor Denver, CO"]


# ── Observations carry the keyword, best rank per (business, keyword) ──


def _install(monkeypatch, handler):
    real = REAL_CLIENT
    monkeypatch.setattr(D.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("harvey.config.load_env", lambda: EnvConfig(
        dataforseo_login="dummy", dataforseo_password="dummy"))


@pytest.mark.asyncio
async def test_rank_observations_name_the_keyword(state, monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, json=ORGANIC_WITH_PACK))
    report = await D.run_discovery(state, load_config(), "dataforseo_serp", [
        D.DiscoveryQuery(term="roofer", location="Denver, CO")], max_spend=1.0)
    assert report.errors == []
    company = await state.get_company_by_domain("kettlemanroofing.com")
    rows = await state.get_observations(company_id=company.id)
    by_code = {r["signal_code"]: r for r in rows}
    assert by_code["SERP_RANK"]["value_num"] == 14
    assert by_code["SERP_RANK"]["value_text"] == "roofer Denver, CO"
    assert by_code["IN_LOCAL_PACK"]["value_num"] == 2
    assert by_code["IN_LOCAL_PACK"]["value_text"] == "roofer Denver, CO"

    # The websiteless pack entry is kept by its Google id.
    bob = await state.get_company_by_external_id("dataforseo:cid:333")
    assert bob is not None and bob.name == "Bob's Roof Fix"


@pytest.mark.asyncio
async def test_each_keyword_gets_its_own_rank_and_duplicates_keep_the_best(state, monkeypatch):
    ranks = {"roofer Denver, CO": [22, 9], "roof repair Denver, CO": [5]}

    def handler(request):
        kw = json.loads(request.content)[0]["keyword"]
        rank = ranks[kw].pop(0)
        return httpx.Response(200, json=_envelope([
            {"type": "organic", "rank_group": rank, "domain": "kettlemanroofing.com",
             "url": "https://kettlemanroofing.com/", "title": "Kettleman Roofing"}]))

    _install(monkeypatch, handler)
    queries = [D.DiscoveryQuery(term=t, location="Denver, CO")
               for t in ("roofer", "roof repair", "roofer")]
    report = await D.run_discovery(state, load_config(), "dataforseo_serp",
                                   queries, max_spend=1.0)
    assert report.new_companies == 1
    company = await state.get_company_by_domain("kettlemanroofing.com")
    rows = await state.get_observations(company_id=company.id, signal_code="SERP_RANK")
    best: dict[str, float] = {}
    for r in rows:
        best[r["value_text"]] = min(best.get(r["value_text"], 999), r["value_num"])
    assert best == {"roofer Denver, CO": 9, "roof repair Denver, CO": 5}
    # One FOUND_IN_SERP per business per run, not per keyword.
    found = await state.get_observations(company_id=company.id, signal_code="FOUND_IN_SERP")
    assert len(found) == 1


@pytest.mark.asyncio
async def test_maps_run_records_maps_rank_and_no_website(state, monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, json=MAPS))
    await D.run_discovery(state, load_config(), "dataforseo_maps", [
        D.DiscoveryQuery(term="roofer", location="Denver, CO")], max_spend=1.0)
    bob = await state.get_company_by_external_id("dataforseo:cid:333")
    codes = {r["signal_code"]: r for r in await state.get_observations(company_id=bob.id)}
    assert codes["MAPS_RANK"]["value_num"] == 7
    assert codes["MAPS_RANK"]["value_text"] == "roofer Denver, CO"
    assert "NO_WEBSITE" in codes and "UNCLAIMED_LISTING" in codes
    assert bob.industry == "Roofer"


# ── Cohort: a numeric range ──


async def _ranked(state, rows):
    ids = {}
    for name, code, rank, kw in rows:
        if name not in ids:
            ids[name] = await state.add_company(
                Company(name=name, domain=f"{name.lower()}.com"))
        await state.add_observation(code, company_id=ids[name], collector="discover",
                                    value_num=rank, value_text=kw)
    return ids


@pytest.mark.asyncio
async def test_cohort_range_selects_rank_11_to_30(state):
    await _ranked(state, [
        ("Top", "SERP_RANK", 3, "roofer Denver"),
        ("Mid", "SERP_RANK", 14, "roofer Denver"),
        ("Edge", "SERP_RANK", 30, "roofer Denver"),
        ("Deep", "SERP_RANK", 45, "roofer Denver"),
    ])
    size, rows = await state.cohort_companies(
        ["SERP_RANK"], [], ranges=[{"code": "SERP_RANK", "min": 11, "max": 30}])
    assert size == 2
    assert sorted(r["name"] for r in rows) == ["Edge", "Mid"]
    # Open-ended bounds.
    size, _ = await state.cohort_companies(
        [], [], ranges=[{"code": "SERP_RANK", "min": 31}])
    assert size == 1


@pytest.mark.asyncio
async def test_cohort_range_reads_each_keywords_latest_rank(state):
    """A company ranks for several searches. Any one in range qualifies, and a
    keyword's newer rank replaces its older one."""
    ids = await _ranked(state, [("Multi", "SERP_RANK", 4, "roofer Denver")])
    await state.add_observation("SERP_RANK", company_id=ids["Multi"], collector="discover",
                                value_num=18, value_text="roof repair Denver",
                                observed_at="2026-01-01 00:00:00")
    size, _ = await state.cohort_companies(
        [], [], ranges=[{"code": "SERP_RANK", "min": 11, "max": 30}])
    assert size == 1
    # Later, that keyword improves to 6: no longer in 11-30.
    await state.add_observation("SERP_RANK", company_id=ids["Multi"], collector="discover",
                                value_num=6, value_text="roof repair Denver",
                                observed_at="2026-06-01 00:00:00")
    size, _ = await state.cohort_companies(
        [], [], ranges=[{"code": "SERP_RANK", "min": 11, "max": 30}])
    assert size == 0


@pytest.mark.asyncio
async def test_cohort_range_combines_with_require_and_exclude(state):
    ids = await _ranked(state, [
        ("Mid", "SERP_RANK", 14, "roofer Denver"),
        ("Pack", "SERP_RANK", 15, "roofer Denver"),
        ("Pack", "IN_LOCAL_PACK", 2, "roofer Denver"),
    ])
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=ids["Mid"],
                                collector="profile", value_num=1)
    size, rows = await state.cohort_companies(
        [], ["IN_LOCAL_PACK"], ranges=[{"code": "SERP_RANK", "min": 11, "max": 30}])
    assert [r["name"] for r in rows] == ["Mid"]
    size, rows = await state.cohort_companies(
        ["RUNNING_GOOGLE_ADS"], [], ranges=[{"code": "SERP_RANK", "max": 20}])
    assert [r["name"] for r in rows] == ["Mid"]


def test_new_signals_are_in_the_catalog():
    codes = {s["code"]: s for s in SIGNAL_CATALOG}
    for code in ("IN_LOCAL_PACK", "MAPS_RANK"):
        assert codes[code]["category"] == "discovery"
        assert codes[code]["value_type"] == "num"


@pytest.mark.asyncio
async def test_a_repeat_search_never_overwrites_a_better_rank(state, monkeypatch):
    """Second run of the same phrase: organic improves, pack slot gets worse.
    Only the improvement is written, so the pack slot's latest stays 1."""
    responses = [
        [{"type": "local_pack", "rank_group": 1, "domain": "kettlemanroofing.com",
          "title": "Kettleman Roofing", "is_paid": False, "cid": "222"},
         {"type": "organic", "rank_group": 20, "domain": "kettlemanroofing.com",
          "url": "https://kettlemanroofing.com/", "title": "Kettleman Roofing"}],
        [{"type": "local_pack", "rank_group": 3, "domain": "kettlemanroofing.com",
          "title": "Kettleman Roofing", "is_paid": False, "cid": "222"},
         {"type": "organic", "rank_group": 12, "domain": "kettlemanroofing.com",
          "url": "https://kettlemanroofing.com/", "title": "Kettleman Roofing"}],
    ]
    _install(monkeypatch, lambda r: httpx.Response(200, json=_envelope(responses.pop(0))))
    q = [D.DiscoveryQuery(term="roofer", location="Denver, CO") for _ in range(2)]
    await D.run_discovery(state, load_config(), "dataforseo_serp", q, max_spend=1.0)
    company = await state.get_company_by_domain("kettlemanroofing.com")
    pack = await state.get_observations(company_id=company.id, signal_code="IN_LOCAL_PACK")
    organic = await state.get_observations(company_id=company.id, signal_code="SERP_RANK")
    assert [r["value_num"] for r in pack] == [1]
    assert sorted(r["value_num"] for r in organic) == [12, 20]


# ── Review fixes: ranks that supersede, expire, and resolve to one company ──


def _organic(rank, domain="kettlemanroofing.com"):
    return {"type": "organic", "rank_group": rank, "domain": domain,
            "url": f"https://{domain}/", "title": "Kettleman Roofing"}


def _pack(slot, cid="222", domain="kettlemanroofing.com"):
    return {"type": "local_pack", "rank_group": slot, "domain": domain,
            "title": "Kettleman Roofing", "is_paid": False, "cid": cid}


async def _run(state, monkeypatch, pages, provider="dataforseo_serp", n=None):
    """One run; each query gets the next page of items."""
    pages = list(pages)
    _install(monkeypatch, lambda r: httpx.Response(200, json=_envelope(pages.pop(0))))
    q = [D.DiscoveryQuery(term="roofer", location="Denver, CO")
         for _ in range(n or len(pages))]
    report = await D.run_discovery(state, load_config(), provider, q, max_spend=1.0)
    assert report.errors == []
    return report


async def _in_range(state, code="SERP_RANK", lo=11, hi=30):
    size, _ = await state.cohort_companies(
        [], [], ranges=[{"code": code, "min": lo, "max": hi}])
    return size


@pytest.mark.asyncio
async def test_a_better_rank_later_in_the_same_run_wins(state, monkeypatch):
    """25 then 5 for one phrase in one run lands in the same second. The
    5 must be what the cohort reads, not a tie with 25."""
    await _run(state, monkeypatch, [[_organic(25)], [_organic(5)]])
    assert await _in_range(state) == 0
    assert await _in_range(state, lo=1, hi=10) == 1


@pytest.mark.asyncio
async def test_unphrased_legacy_ranks_give_way_to_phrased_ones(state):
    cid = await state.add_company(Company(name="Old", domain="old.com"))
    await state.add_observation("SERP_RANK", company_id=cid, collector="discover",
                                value_num=15, observed_at="2026-01-01 00:00:00")
    # Before any phrased rank exists the legacy row still counts.
    assert await _in_range(state) == 1
    await state.add_observation("SERP_RANK", company_id=cid, collector="discover",
                                value_num=2, value_text="roofer Denver, CO")
    assert await _in_range(state) == 0


@pytest.mark.asyncio
async def test_dropping_out_of_the_results_clears_the_rank(state, monkeypatch):
    await _run(state, monkeypatch, [[_organic(14), _pack(2)]])
    company = await state.get_company_by_domain("kettlemanroofing.com")
    assert await _in_range(state) == 1
    size, _ = await state.cohort_companies(["SERP_RANK"], ["IN_LOCAL_PACK"])
    assert size == 0

    # Next run: same search, Kettleman is gone from both organic and pack.
    await _run(state, monkeypatch, [[_organic(3, domain="northvaleroofing.com")]])
    assert await _in_range(state) == 0
    present, _ = await state.cohort_companies(["SERP_RANK"], [])
    assert present == 1, "only Northvale still ranks"
    pack, _ = await state.cohort_companies(["IN_LOCAL_PACK"], [])
    assert pack == 0, "a lost pack slot must not keep excluding the business"
    marks = await state.get_observations(company_id=company.id, signal_code="SERP_RANK")
    assert {r["value_text"] for r in marks} == {"roofer Denver, CO"}


@pytest.mark.asyncio
async def test_a_rank_for_another_phrase_is_untouched(state, monkeypatch):
    await _ranked(state, [("Kettleman", "SERP_RANK", 18, "roof repair Denver")])
    await _run(state, monkeypatch, [[_organic(3, domain="northvaleroofing.com")]])
    assert await _in_range(state) == 1


@pytest.mark.asyncio
async def test_a_business_ranked_earlier_in_the_run_is_not_marked_gone(state, monkeypatch):
    """Two queries for one phrase: ranked in the first, missing in the second.
    The run's best rank stands."""
    await _run(state, monkeypatch, [[_organic(14)], [_organic(3, domain="northvaleroofing.com")]])
    assert await _in_range(state) == 1


@pytest.mark.parametrize("order", ["pack_first", "maps_first"])
@pytest.mark.asyncio
async def test_pack_and_maps_resolve_to_one_websiteless_company(state, monkeypatch, order):
    pack = _envelope([_pack(3, cid="333", domain=None) | {"title": "Bob's Roof Fix"}])
    maps = MAPS
    first, second = (pack, maps) if order == "pack_first" else (maps, pack)
    for payload, key in ((first, "dataforseo_serp" if first is pack else "dataforseo_maps"),
                         (second, "dataforseo_serp" if second is pack else "dataforseo_maps")):
        _install(monkeypatch, lambda r, p=payload: httpx.Response(200, json=p))
        report = await D.run_discovery(state, load_config(), key, [
            D.DiscoveryQuery(term="roofer", location="Denver, CO")], max_spend=1.0)
        assert report.errors == [], report.errors
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(state.db_path)) as db:
        names = [r[0] for r in db.execute("SELECT name FROM companies")]
    assert names.count("Bob's Roof Fix") == 1, names


@pytest.mark.asyncio
async def test_a_place_id_keyed_company_is_found_by_its_alias(state, monkeypatch):
    """Rows stored before this change were keyed by place_id."""
    await state.add_company(Company(name="Bob's Roof Fix", external_id="dataforseo:ChIJbob"))
    report = await _run(state, monkeypatch, [MAPS["tasks"][0]["result"][0]["items"]],
                        provider="dataforseo_maps")
    assert report.known_companies == 1 and report.new_companies == 1


@pytest.mark.asyncio
async def test_range_respects_confidence_like_require(state):
    cid = await state.add_company(Company(name="Neg", domain="neg.com"))
    await state.add_observation("SERP_RANK", company_id=cid, collector="discover",
                                value_num=15, value_text="roofer Denver", confidence=-1)
    req, _ = await state.cohort_companies(["SERP_RANK"], [])
    assert req == 0
    assert await _in_range(state) == 0
