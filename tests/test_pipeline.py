"""DISCOVER → PROFILE chaining.

The stages are joined by a query, not a hand-off. These tests pin that
property down, because it is what makes a crashed run resumable and
re-observation a time series instead of a duplicate.
"""

import os
import tempfile

import pytest
import pytest_asyncio

from harvey import pipeline as P
from harvey.collectors import discover as D
from harvey.config import load_config
from harvey.models import Company
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


# ── The join: which companies still need a look ──


@pytest.mark.asyncio
async def test_a_new_company_needs_profiling(state):
    await state.add_company(Company(name="Baker", domain="baker.com"))
    pending = await state.companies_needing_profile()
    assert [c["domain"] for c in pending] == ["baker.com"]
    assert await state.count_companies_needing_profile() == 1


@pytest.mark.asyncio
async def test_a_profiled_company_drops_out(state):
    cid = await state.add_company(Company(name="Baker", domain="baker.com"))
    await state.add_observation("TECH_STACK", company_id=cid,
                                collector="profile", value_text="wordpress")
    assert await state.count_companies_needing_profile() == 0


@pytest.mark.asyncio
async def test_a_stale_profile_comes_back_round(state):
    """Re-reading on a schedule is what makes 'they dropped their agency'
    visible. A company profiled last year is not done forever."""
    cid = await state.add_company(Company(name="Baker", domain="baker.com"))
    await state.add_observation("TECH_STACK", company_id=cid, collector="profile",
                                value_text="wordpress",
                                observed_at="2020-01-01 00:00:00")
    assert await state.count_companies_needing_profile(stale_days=90) == 1
    assert await state.count_companies_needing_profile(stale_days=99999) == 0


@pytest.mark.asyncio
async def test_discovery_observations_do_not_count_as_a_profile(state):
    """DISCOVER writes observations too. Only PROFILE's own count."""
    cid = await state.add_company(Company(name="Baker", domain="baker.com"))
    await state.add_observation("SERP_RANK", company_id=cid,
                                collector="discover", value_num=14)
    assert await state.count_companies_needing_profile() == 1


@pytest.mark.asyncio
async def test_websiteless_companies_are_never_queued_for_profiling(state):
    """No site to read. That is the NO_WEBSITE cohort, not a backlog."""
    await state.add_company(Company(name="No Site", external_id="osm:node/1"))
    assert await state.count_companies_needing_profile() == 0


# ── The chain ──


class FakeDiscovery(D.DiscoveryProvider):
    key = "fake"
    label = "Fake"

    def __init__(self, businesses):
        self.businesses = businesses

    def configured(self, env):
        return True

    def estimate(self, queries):
        return 0.0

    async def fetch(self, client, env, query):
        return self.businesses


@pytest.fixture
def chain(monkeypatch):
    """A fake discovery provider and a profile stage that records its input."""
    seen = {"profiled": []}

    def install(businesses):
        monkeypatch.setitem(D.PROVIDERS, "fake", FakeDiscovery(businesses))

        async def fake_profile(state, companies, **_):
            seen["profiled"] = [c["domain"] for c in companies]
            for company in companies:
                await state.add_observation(
                    "TECH_STACK", company_id=company["id"],
                    collector="profile", value_text="wordpress")
            return len(companies), "run-x"

        monkeypatch.setattr("harvey.collectors.profile.profile_companies",
                            fake_profile)
        return seen

    async def offline(state, city, radius_km=40, client=None):
        return ""
    monkeypatch.setattr(D, "geocode_city", offline)
    return install


@pytest.mark.asyncio
async def test_discovery_flows_straight_into_profiling(state, config, chain):
    seen = chain([
        D.Business(name="Baker", domain="baker.com", provider="fake"),
        D.Business(name="Metro", domain="metro.com", provider="fake"),
    ])
    report = await P.run_prospecting(
        state, config, "fake", [D.DiscoveryQuery(term="roofer")])

    assert report.discover["new_companies"] == 2
    assert sorted(seen["profiled"]) == ["baker.com", "metro.com"]
    assert report.profiled_companies == 2
    assert report.profile_observations == 2


@pytest.mark.asyncio
async def test_profiling_sweeps_up_what_an_earlier_run_left(state, config, chain):
    """The stages join through a query, so a run interrupted before profiling
    is finished by the next one — nothing has to remember anything."""
    await state.add_company(Company(name="Left Over", domain="leftover.com"))
    seen = chain([D.Business(name="Baker", domain="baker.com", provider="fake")])

    await P.run_prospecting(state, config, "fake", [D.DiscoveryQuery(term="roofer")])
    assert sorted(seen["profiled"]) == ["baker.com", "leftover.com"]


@pytest.mark.asyncio
async def test_profiling_runs_even_when_discovery_finds_nothing(state, config, chain):
    await state.add_company(Company(name="Left Over", domain="leftover.com"))
    seen = chain([])

    report = await P.run_prospecting(state, config, "fake",
                                     [D.DiscoveryQuery(term="roofer")])
    assert report.discover["new_companies"] == 0
    assert seen["profiled"] == ["leftover.com"]


@pytest.mark.asyncio
async def test_no_profile_stops_at_discovery(state, config, chain):
    seen = chain([D.Business(name="Baker", domain="baker.com", provider="fake")])
    report = await P.run_prospecting(state, config, "fake",
                                     [D.DiscoveryQuery(term="roofer")],
                                     profile=False)
    assert report.discover["new_companies"] == 1
    assert seen["profiled"] == []
    assert report.skipped == "profiling skipped"


@pytest.mark.asyncio
async def test_a_dry_run_neither_discovers_nor_profiles(state, config, chain):
    seen = chain([D.Business(name="Baker", domain="baker.com", provider="fake")])
    report = await P.run_prospecting(state, config, "fake",
                                     [D.DiscoveryQuery(term="roofer")],
                                     dry_run=True)
    assert report.skipped == "dry run"
    assert seen["profiled"] == []
    assert await state.count_companies_needing_profile() == 0


@pytest.mark.asyncio
async def test_a_failing_profile_stage_does_not_lose_the_discovery(
        state, config, monkeypatch, chain):
    chain([D.Business(name="Baker", domain="baker.com", provider="fake")])

    async def boom(state, companies, **_):
        raise RuntimeError("network gone")
    monkeypatch.setattr("harvey.collectors.profile.profile_companies", boom)

    report = await P.run_prospecting(state, config, "fake",
                                     [D.DiscoveryQuery(term="roofer")])
    assert report.discover["new_companies"] == 1, "the companies are still saved"
    assert any("network gone" in e for e in report.errors)


@pytest.mark.asyncio
async def test_profile_stage_is_a_noop_with_nothing_to_do(state):
    companies, observations, run_id = await P.run_profile_stage(state)
    assert (companies, observations, run_id) == (0, 0, "")


@pytest.mark.asyncio
async def test_state_summary_reports_the_profiling_backlog(state):
    """The heartbeat reads this to decide whether to profile this cycle."""
    await state.add_company(Company(name="Baker", domain="baker.com"))
    summary = await state.get_state_summary()
    assert summary["unprofiled"] == 1


# ── Cohorts read the value, not just the row ──


@pytest.mark.asyncio
async def test_a_false_boolean_is_recorded_but_not_in_the_cohort(state):
    """PROFILE records RUNNING_GOOGLE_ADS=0 for everyone it checks — knowing
    you looked is worth keeping, and a later 0→1 flip is a real event. But
    "companies running Google Ads" must not silently mean "companies we
    checked for Google Ads", which is everyone.
    """
    running = await state.add_company(Company(name="Spender", domain="a.com"))
    checked = await state.add_company(Company(name="Not spending", domain="b.com"))
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=running,
                                collector="profile", value_num=1)
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=checked,
                                collector="profile", value_num=0)

    assert await state.cohort(["RUNNING_GOOGLE_ADS"]) == [running]

    counts = {c["signal_code"]: c for c in await state.signal_counts()}
    assert counts["RUNNING_GOOGLE_ADS"]["companies"] == 1

    # The negative finding is still on disk.
    rows = await state.get_observations(company_id=checked)
    assert rows[0]["value_num"] == 0


@pytest.mark.asyncio
async def test_text_signals_count_by_their_existence(state):
    """INCUMBENT_AGENCY has no numeric value — the row IS the finding."""
    cid = await state.add_company(Company(name="Baker", domain="a.com"))
    await state.add_observation("INCUMBENT_AGENCY", company_id=cid,
                                collector="profile", value_text="Studio3")
    assert await state.cohort(["INCUMBENT_AGENCY"]) == [cid]


@pytest.mark.asyncio
async def test_a_cohort_reflects_the_newest_observation(state):
    """They dropped their agency last quarter. The cohort should say so."""
    cid = await state.add_company(Company(name="Baker", domain="a.com"))
    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=cid,
                                collector="profile", value_num=1,
                                observed_at="2026-01-01 00:00:00")
    assert await state.cohort(["RUNNING_GOOGLE_ADS"]) == [cid]

    await state.add_observation("RUNNING_GOOGLE_ADS", company_id=cid,
                                collector="profile", value_num=0,
                                observed_at="2026-06-01 00:00:00")
    assert await state.cohort(["RUNNING_GOOGLE_ADS"]) == []

    # Both observations survive — that time series is the durable asset.
    assert len(await state.get_observations(company_id=cid)) == 2


@pytest.mark.asyncio
async def test_exclusions_also_read_the_value(state):
    """Excluding "has online booking" must not exclude everyone who was
    merely checked for it."""
    a = await state.add_company(Company(name="A", domain="a.com"))
    b = await state.add_company(Company(name="B", domain="b.com"))
    for cid, booking in ((a, 1), (b, 0)):
        await state.add_observation("NO_SCHEMA_MARKUP", company_id=cid,
                                    collector="profile", value_num=1)
        await state.add_observation("NO_ONLINE_BOOKING", company_id=cid,
                                    collector="profile", value_num=booking)

    both = await state.cohort(["NO_SCHEMA_MARKUP"])
    assert sorted(both) == sorted([a, b])

    # Only A actually lacks booking, so excluding it leaves B.
    assert await state.cohort(["NO_SCHEMA_MARKUP"], exclude=["NO_ONLINE_BOOKING"]) == [b]
