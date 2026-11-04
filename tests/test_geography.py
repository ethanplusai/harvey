"""icp.geography order is priority: earlier entries are written to first."""

import asyncio
from types import SimpleNamespace

from harvey.agents.writer import Writer
from harvey.fit import company_fit
from harvey.geography import matches, rank, tier


def config(geography=("Denver, CO", "United States", "Canada"), coords=None):
    return SimpleNamespace(icp=SimpleNamespace(
        geography=list(geography),
        geo_coordinates=coords if coords is not None else {"Denver, CO": "39.7392,-104.9903,50"},
        industries=[], titles=[], hiring_signals=[],
    ), compliance=SimpleNamespace(do_not_contact=[]))


def test_city_entry_matches_by_name():
    assert tier("Denver, CO", config()) == (0, "Denver, CO")
    assert tier("Denver, Colorado, USA", config()) == (0, "Denver, CO")


def test_city_entry_matches_inside_its_radius():
    # A suburb ~15 km out, known only by its cached coordinates.
    assert tier("Lakewood, CO", config(), point="39.7047,-105.0814") == (0, "Denver, CO")
    # Same state, outside the 50 km circle: falls to the country tier.
    assert tier("Pueblo, CO", config(), point="38.2544,-104.6091") == (1, "United States")


def test_country_entries_match_by_country():
    assert tier("Austin, TX", config()) == (1, "United States")
    assert tier("US", config()) == (1, "United States")
    assert tier("Toronto, ON", config()) == (2, "Canada")
    assert tier("Canada", config()) == (2, "Canada")


def test_same_city_name_in_another_country_does_not_match():
    assert not matches("London, UK", "London, ON, Canada")
    assert matches("London, UK", "London, United Kingdom")


def test_outside_every_entry_ranks_last_but_is_kept():
    cfg = config()
    assert tier("Lyon, France", cfg) == (None, "")
    assert rank("Lyon, France", cfg) == 3
    assert rank("", cfg) == 3
    assert rank("Denver, CO", cfg) < rank("Austin, TX", cfg) < rank("Toronto, ON", cfg) < rank("FR", cfg)


def test_no_geography_ranks_everyone_equal():
    cfg = config(geography=(), coords={})
    assert rank("Denver, CO", cfg) == rank("Lyon, France", cfg) == 0


def test_region_names_match_only_by_name():
    cfg = config(geography=("Western Europe",), coords={})
    assert tier("Western Europe", cfg) == (0, "Western Europe")
    assert tier("Lyon, France", cfg) == (None, "")


def test_fit_reports_location_without_changing_the_score():
    company = {"id": "c1", "domain": "acme.example", "industry": "", "location": "Toronto, ON"}
    score, reasons = company_fit(company, [], [], config())
    assert score == 0
    assert "location: Canada (priority 3)" in reasons
    far = {**company, "location": "Lyon, France"}
    assert company_fit(far, [], [], config())[1][-1] == "location: outside your geography (contacted last)"
    assert not any(r.startswith("location") for r in company_fit({**company, "location": ""}, [], [], config())[1])


class FakeState:
    def __init__(self, places):
        self.places = places

    async def get_company(self, company_id):
        return SimpleNamespace(location=self.places.get(company_id, ""))

    async def get_setting(self, key):
        return None


def test_writer_orders_by_tier_then_score():
    writer = Writer.__new__(Writer)
    writer.config = config()
    writer.state = FakeState({"far": "Lyon, France", "us": "Austin, TX", "home": "Denver, CO", "ca": "Toronto, ON"})
    p = lambda pid, cid, score: SimpleNamespace(id=pid, company_id=cid, score=score)
    prospects = [p("1", "far", 99), p("2", "us", 40), p("3", "home", 10), p("4", "us", 80),
                 p("5", "ca", 90), p("6", "", 95)]
    ordered = asyncio.run(writer._prioritise(prospects))
    assert [x.id for x in ordered] == ["3", "4", "2", "5", "1", "6"]
