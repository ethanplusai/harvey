"""icp.require_location: Scout works only with companies whose location is known.

Scout's own search by industry has no city, so it finds companies anywhere.
For someone selling in a few places, one of those reaching the Outbox is a
mistake. With the option on, Scout skips that search, job-board discovery
and any known company with no location; `sellbird discover` sets a company's
location from the city it searched. Off by default.
"""

from types import SimpleNamespace

import pytest

from sellbird.agents.scout import Scout
from sellbird.config import ComplianceConfig, ICPConfig
from sellbird.models.company import Company
from sellbird.state import StateManager


class FakeState:
    def __init__(self, companies):
        self.companies = companies
        self.checked = []
        self.settings = {}

    async def companies_needing_prospects(self, limit=10, **kw):
        return self.companies[:limit]

    async def mark_prospects_checked(self, company_id):
        self.checked.append(company_id)

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def increment_setting(self, key):
        self.settings[key] = str(int(self.settings.get(key, "0")) + 1)


def make_scout(companies, require_location=None):
    icp = dict(titles=["Owner"], industries=["Bakery"], geography=["Exampleville"])
    if require_location is not None:
        icp["require_location"] = require_location
    config = SimpleNamespace(
        icp=SimpleNamespace(**icp),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)),
        compliance=ComplianceConfig(),
    )
    scout = Scout(brain=None, state=FakeState(companies), config=config,
                  env=SimpleNamespace(hunter_api_key=""))
    scout._seen_domains = set()
    scout._seen_prospect_keys = set()
    scout.contacted = []

    async def contacts(company_id, domain, **kw):
        scout.contacted.append(domain)
        return []

    scout._contacts_from_company = contacts
    return scout


PLACED = {"id": "c1", "domain": "placed.example", "name": "Placed", "industry": "Bakery",
          "location": "Exampleville, Testland"}
UNPLACED = {"id": "c2", "domain": "unplaced.example", "name": "Unplaced", "industry": "Bakery",
            "location": ""}


def test_the_option_is_off_by_default():
    icp = dict(industries=["x"], company_size="1-10", titles=["Owner"], geography=["y"])
    assert ICPConfig(**icp).require_location is False
    assert ICPConfig(**icp, require_location=True).require_location is True


@pytest.mark.asyncio
async def test_on_a_known_company_without_a_location_is_skipped_and_marked_checked():
    scout = make_scout([PLACED, UNPLACED], require_location=True)
    await scout._prospect_via_known_companies()
    assert scout.contacted == ["placed.example"]
    assert sorted(scout.state.checked) == ["c1", "c2"]   # the rotation moves past both


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [None, False])
async def test_off_every_known_company_is_looked_at(setting):
    scout = make_scout([PLACED, UNPLACED], require_location=setting)
    await scout._prospect_via_known_companies()
    assert scout.contacted == ["placed.example", "unplaced.example"]


@pytest.mark.asyncio
async def test_on_the_search_by_industry_does_not_run():
    scout = make_scout([], require_location=True)
    searched = []

    async def search():
        searched.append("search")
        return [{"domain": "elsewhere.example", "name": "Elsewhere"}]

    async def directories():
        searched.append("directories")
        return []

    scout._find_companies_via_search = search
    scout._find_companies_via_directories = directories
    assert await scout._prospect_via_company_discovery() == 0
    assert searched == []


@pytest.mark.asyncio
async def test_off_the_search_by_industry_still_runs():
    scout = make_scout([], require_location=False)
    searched = []

    async def search():
        searched.append("search")
        return []

    async def directories():
        return []

    scout._find_companies_via_search = search
    scout._find_companies_via_directories = directories
    await scout._prospect_via_company_discovery()
    assert searched == ["search"]


@pytest.mark.asyncio
async def test_on_job_board_discovery_does_not_run():
    assert await make_scout([], require_location=True)._prospect_via_hiring_boards() == 0


@pytest.mark.asyncio
async def test_the_state_hands_scout_the_location(tmp_path):
    state = StateManager(str(tmp_path / "s.db"))
    await state.init_db()
    await state.add_company(Company(name="Placed", domain="placed.example",
                                    location="Exampleville, Testland"))
    rows = await state.companies_needing_prospects(limit=5)
    assert [r["location"] for r in rows] == ["Exampleville, Testland"]
