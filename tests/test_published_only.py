"""channels.email.published_only: cold email only to an address the company
printed on its own website, and the page that printed it travels with it."""

import pytest

from harvey.agents import scout as scout_mod
from harvey.gate import UNPUBLISHED_ADDRESS, pre_send_check
from harvey.models.prospect import Prospect
from tests.test_company_inbox import PAGES, make_scout
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg, FakeProvider, make_sender, seed_campaign, state,
)

PAGE = "https://acme.example/contact"


def _prospect(source_url=""):
    return Prospect(first_name="Jane", last_name="Doe", title="Property Manager",
                    email="jane@acme.example", email_status="verified",
                    email_source_url=source_url)


# ── gate ──

def test_off_by_default_a_pattern_address_still_passes():
    r = pre_send_check("jane@acme.example", "s", "Hello there.", _prospect())
    assert UNPUBLISHED_ADDRESS not in r.reasons


def test_pattern_address_is_blocked_when_on():
    r = pre_send_check("jane@acme.example", "s", "Hello there.", _prospect(),
                       published_only=True)
    assert UNPUBLISHED_ADDRESS in r.reasons


def test_published_address_passes_when_on():
    r = pre_send_check("jane@acme.example", "s", "Hello there.", _prospect(PAGE),
                       published_only=True)
    assert r.ok


def test_replies_are_not_held():
    r = pre_send_check("jane@acme.example", "Re: hi", "Thanks!", _prospect(),
                       kind="reply", published_only=True)
    assert UNPUBLISHED_ADDRESS not in r.reasons


# ── scout records the page ──

@pytest.mark.asyncio
async def test_inbox_prospect_carries_the_page_that_published_it(monkeypatch):
    async def graded(email, env=None):
        return "verified"

    monkeypatch.setattr(scout_mod, "verify_published_address", graded)
    p = await make_scout(PAGES)._company_inbox_prospect("c1", "acme.com", "Acme", "Property Management")
    assert p.email == "office@acme.com"
    assert p.email_source_url == "https://acme.com/contact"


def test_an_address_never_seen_on_the_site_has_no_page():
    scout = make_scout(PAGES)
    assert scout._published_url("acme.com", "jane.doe@acme.com") == ""


# ── state ──

@pytest.mark.asyncio
async def test_page_is_stored_and_dropped_when_the_address_changes(state):
    pid = await state.add_prospect(_prospect(PAGE))
    assert (await state.get_prospect(pid)).email_source_url == PAGE

    await state.update_prospect_email(pid, "jane@acme.example", "invalid")   # a bounce
    assert (await state.get_prospect(pid)).email_source_url == PAGE

    await state.update_prospect_email(pid, "j.doe@acme.example", "verified")
    assert (await state.get_prospect(pid)).email_source_url == ""


# ── staging ──

@pytest.mark.asyncio
async def test_staging_holds_back_only_unpublished_addresses(state):
    published = await state.add_prospect(_prospect(PAGE))
    guessed = await state.add_prospect(Prospect(
        first_name="Sam", last_name="Roe", title="Facility Manager",
        email="sam@acme.example", email_status="verified"))
    await seed_campaign(state, [published, guessed])
    Cfg.channels.email.published_only = True
    try:
        await make_sender(state, FakeProvider())._run_native()
    finally:
        del Cfg.channels.email.published_only
    staged = {i["to_email"] for i in await state.get_outbox(status="pending_review")}
    assert staged == {"jane@acme.example"}
