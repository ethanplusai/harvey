"""Maharashtra first: discovery order and the Writer's work order.

The operator asked for the first wave of mail to go to Maharashtra and the
rest of India to follow. Two things have to make that true:

1. Discovery searches the priority places before the rest.
2. The Writer drafts for prospects in the priority places first.

Both are pinned here, along with the rule that a company failing the ICP
filter never reaches the Writer at all — whatever its address looks like.
"""

import os
import tempfile

import pytest
import pytest_asyncio

from sellbird.collectors.discover import build_queries, prioritize_places
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

PRIORITY = ["Nashik, India", "Pune, India", "Mumbai, India"]


# ── prioritize_places ──────────────────────────────────────────────────


def test_the_priority_places_come_first():
    places = ["Coimbatore", "Pune, India", "Chennai", "Nashik, India"]
    assert prioritize_places(places, PRIORITY) == [
        "Nashik, India", "Pune, India", "Coimbatore", "Chennai"]


def test_a_place_is_never_lost_or_duplicated():
    places = ["Nashik, India", "Pune, India", "Coimbatore"]
    out = prioritize_places(places, PRIORITY)
    assert sorted(out) == sorted(places)
    assert len(out) == len(set(out))


def test_a_priority_place_is_not_added_when_it_is_not_in_the_list():
    """Priority says which of the operator's places to reach first.

    It does not widen the set of places to search — that is the geography
    list's job, and a test that overrides geography must not have its
    cities quietly changed underneath it.
    """
    places = ["Denver, CO", "Boulder, CO"]
    assert prioritize_places(places, PRIORITY) == ["Denver, CO", "Boulder, CO"]


def test_a_place_matches_by_city_not_by_whole_string():
    places = ["Pune", "Chennai"]
    assert prioritize_places(places, PRIORITY)[0] == "Pune"


def test_no_priority_leaves_the_order_alone():
    places = ["Chennai", "Pune"]
    assert prioritize_places(places, []) == ["Chennai", "Pune"]


# ── build_queries uses the priority order ──────────────────────────────


def test_discovery_searches_the_priority_places_first():
    from sellbird.config import load_config

    config = load_config()
    config.icp.industries = ["polymer manufacturer"]
    config.icp.geography = ["Nashik, India", "Coimbatore", "Pune, India"]
    config.icp.priority_geography = PRIORITY
    config.icp.keyword_variants = {}

    queries = [q.keyword() for q in build_queries(config)]
    assert queries == [
        "polymer manufacturer Nashik, India",
        "polymer manufacturer Pune, India",
        "polymer manufacturer Coimbatore",
    ]


# ── get_prospects_to_write ─────────────────────────────────────────────


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "t.db"))
        await sm.init_db()
        yield sm


async def _add_company(state, name, domain, industry, location):
    cid = await state.add_company(Company(
        name=name, domain=domain, industry=industry, location=location))
    return cid


async def _add_prospect(state, name, email, company_id="", location="", score=50):
    return await state.add_prospect(Prospect(
        first_name="Pat", last_name="Shah", title="Owner", company=name,
        industry="polymer manufacturer", email=email, email_status="verified",
        company_id=company_id, location=location, score=score))


@pytest.mark.asyncio
async def test_maharashtra_prospects_are_drafted_for_first(state):
    await _add_prospect(state, "Elsewhere Co", "a@elsewhere.test",
                        location="Coimbatore")
    await _add_prospect(state, "Pune Co", "a@puneco.test", location="Pune")

    out = await state.get_prospects_to_write(priority_locations=PRIORITY)
    assert [p.company for p in out] == ["Pune Co", "Elsewhere Co"]


@pytest.mark.asyncio
async def test_a_prospect_with_no_location_falls_back_to_its_company(state):
    cid = await _add_company(state, "Nashik Works", "nashikworks.test",
                              "polymer manufacturer", "Nashik")
    await _add_prospect(state, "Nashik Works", "a@nashikworks.test",
                        company_id=cid)

    out = await state.get_prospects_to_write(priority_locations=PRIORITY)
    assert [p.company for p in out] == ["Nashik Works"]


@pytest.mark.asyncio
async def test_within_a_place_the_best_score_comes_first(state):
    await _add_prospect(state, "Low", "low@pune.test", location="Pune", score=20)
    await _add_prospect(state, "High", "high@pune.test", location="Pune", score=90)

    out = await state.get_prospects_to_write(priority_locations=PRIORITY)
    assert [p.company for p in out] == ["High", "Low"]


@pytest.mark.asyncio
async def test_a_vendor_is_never_offered_to_the_writer(state):
    """The filter is the last line of defence.

    Even if a vendor is already on file with a deliverable address, the
    Writer must not draft for it.
    """
    cid = await _add_company(state, "Delta Sales App", "deltasalesapp.test",
                              "Software", "Bengaluru")
    await _add_prospect(state, "Delta Sales App", "info@deltasalesapp.test",
                        company_id=cid, location="Bengaluru")

    assert await state.get_prospects_to_write(priority_locations=PRIORITY) == []


@pytest.mark.asyncio
async def test_a_trading_company_is_never_offered_to_the_writer(state):
    cid = await _add_company(state, "M Y Trading Co.", "mytrading.test",
                              "trading company", "Pune")
    await _add_prospect(state, "M Y Trading Co.", "info@mytrading.test",
                        company_id=cid, location="Pune")

    assert await state.get_prospects_to_write(priority_locations=PRIORITY) == []


@pytest.mark.asyncio
async def test_a_blog_post_as_a_company_is_never_offered_to_the_writer(state):
    cid = await _add_company(state, "Manufacturing Companies in Nagpur",
                              "nagpuronline.test", "Manufacturing", "Nagpur")
    await _add_prospect(state, "Manufacturing Companies in Nagpur",
                        "info@nagpuronline.test", company_id=cid,
                        location="Nagpur")

    assert await state.get_prospects_to_write(priority_locations=PRIORITY) == []


@pytest.mark.asyncio
async def test_only_new_prospects_are_offered(state):
    await _add_prospect(state, "Fresh", "fresh@acme.test", location="Pune")
    contacted = await _add_prospect(state, "Done", "done@acme.test",
                                    location="Pune")
    await state.update_prospect_status(contacted, "contacted")

    out = await state.get_prospects_to_write(priority_locations=PRIORITY)
    assert [p.company for p in out] == ["Fresh"]


@pytest.mark.asyncio
async def test_a_prospect_records_its_company_location(state):
    """The location is copied from the company, not guessed."""
    cid = await _add_company(state, "Nashik Works", "nashikworks.test",
                              "polymer manufacturer", "Nashik, India")
    pid = await _add_prospect(state, "Nashik Works", "a@nashikworks.test",
                              company_id=cid)
    prospect = await state.get_prospect(pid)
    assert prospect.location == "Nashik, India"
