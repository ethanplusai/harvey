"""Scout records the place it searched, so the Writer can put priority places first.

Discovery already stamps the place onto the company it found. Scout's own
searches did not: a company found by a "Nashik" search arrived with no
location, and the Writer's priority ordering (icp.priority_geography) skips a
prospect whose place is unknown — silently, because an empty place is a
legitimate answer for a company found some other way.

These tests hold Scout to the discovery rule: the place a search was scoped to
is the place its results are recorded in, and a company that predates the
column gets filled in rather than left blank.
"""

from types import SimpleNamespace

import pytest

from sellbird.agents.scout import Scout


NASHIK = "Nashik, India"
PUNE = "Pune, India"


class FakeState:
    def __init__(self, existing=None):
        self.existing = existing
        self.filled = []

    async def company_exists(self, domain):
        return False

    async def get_company_by_domain(self, domain):
        return self.existing

    async def set_company_location(self, company_id, location):
        self.filled.append((company_id, location))

    async def add_company(self, company):
        return "new-" + company.domain


def make_scout(priority=(NASHIK, PUNE), geography=(NASHIK, PUNE, "Ludhiana, India")):
    config = SimpleNamespace(
        icp=SimpleNamespace(
            titles=["Owner"],
            industries=["Manufacturing"],
            company_size="20-500 employees",
            geography=list(geography),
            priority_geography=list(priority),
        ),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)),
    )
    scout = Scout(brain=None, state=FakeState(), config=config, env=SimpleNamespace())
    scout._seen_domains = set()
    scout._seen_prospect_keys = set()
    scout._queries_this_cycle = 0
    return scout


def test_a_priority_place_leads_the_company_search():
    scout = make_scout()
    queries = scout._build_company_search_queries()
    assert queries, "expected at least one search"
    assert queries[0][1] == NASHIK
    assert all(place == NASHIK for _, place in queries)


def test_without_a_priority_list_the_first_place_is_searched():
    scout = make_scout(priority=())
    queries = scout._build_company_search_queries()
    assert queries[0][1] == NASHIK  # geography[0]


@pytest.mark.asyncio
async def test_a_company_found_by_a_place_search_records_the_place():
    scout = make_scout()

    async def search(query):
        return [("https://acme.example", "Acme makes things")]

    async def info(domain):
        return {"name": "Acme"}

    scout._web_search = search
    scout._scrape_company_info = info

    companies = await scout._find_companies_via_search()
    assert companies[0]["location"] == NASHIK


@pytest.mark.asyncio
async def test_the_place_scraped_off_the_site_is_not_overwritten():
    scout = make_scout()

    async def search(query):
        return [("https://acme.example", "")]

    async def info(domain):
        # The company's own page said where it is; that beats the search.
        return {"name": "Acme", "location": "Satpur, Nashik"}

    scout._web_search = search
    scout._scrape_company_info = info

    companies = await scout._find_companies_via_search()
    assert companies[0]["location"] == "Satpur, Nashik"


@pytest.mark.asyncio
async def test_an_existing_company_without_a_place_is_filled_in():
    existing = SimpleNamespace(id="c1", location="")
    scout = make_scout()
    scout.state = FakeState(existing)

    company_id = await scout._ensure_company(
        name="Acme", domain="acme.example", location=NASHIK
    )

    assert company_id == "c1"
    assert scout.state.filled == [("c1", NASHIK)]


@pytest.mark.asyncio
async def test_an_existing_company_that_already_has_a_place_is_left_alone():
    existing = SimpleNamespace(id="c1", location=PUNE)
    scout = make_scout()
    scout.state = FakeState(existing)

    company_id = await scout._ensure_company(
        name="Acme", domain="acme.example", location=NASHIK
    )

    assert company_id == "c1"
    assert scout.state.filled == []
