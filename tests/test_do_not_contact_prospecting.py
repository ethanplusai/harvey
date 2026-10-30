"""A company on `compliance.do_not_contact` costs nothing past discovery.

The send gate holds every email to a listed domain, so anything Scout does
for one (crawling its team pages, verifying candidate addresses against a
small free verifier allowance) and any draft the Writer writes for it is
spend nobody can use. Discovery already skips listed domains; a domain added
to the list later, for a company already on file, must be skipped too, from
the next cycle on and without a restart.
"""

import shutil
from types import SimpleNamespace

import pytest
import pytest_asyncio

import harvey.agents.scout as scout_mod
import harvey.integrations.email_finder as ef
from harvey.agents.scout import Scout
from harvey.agents.writer import DO_NOT_CONTACT_STATUS, Writer
from harvey.config import ComplianceConfig, load_config
from harvey.main import decide_next_action
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager
from tests.test_writer_retry import GOOD, FakeBrain


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "dnc.db"))
    await state.init_db()
    return state


def _config(entries=()):
    return SimpleNamespace(
        icp=SimpleNamespace(titles=["Owner"], industries=["Roofing"], geography=[]),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)),
        compliance=ComplianceConfig(do_not_contact=list(entries)),
    )


def _scout(state, config):
    scout = Scout(brain=None, state=state, config=config, env=SimpleNamespace())
    scout.crawled = []

    async def contacts(company_id, domain, **kw):
        scout.crawled.append(domain)
        return []

    scout._contacts_from_company = contacts
    return scout


async def _company(state, domain):
    return await state.add_company(Company(name=domain.split(".")[0].title(),
                                           domain=domain, industry="Roofing"))


async def _checked(state, company_id):
    async with state._connect() as db:
        async with db.execute("SELECT prospects_checked_at FROM companies WHERE id = ?",
                              (company_id,)) as cur:
            return (await cur.fetchone())[0]


# ── Scout ──

@pytest.mark.asyncio
async def test_the_backfill_never_crawls_a_listed_company(state):
    listed = await _company(state, "acme.example")
    await _company(state, "other.example")
    scout = _scout(state, _config(["acme.example"]))
    await scout._prospect_via_known_companies()
    assert scout.crawled == ["other.example"]
    # Marked checked, so it rotates out instead of taking a slot every cycle.
    assert await _checked(state, listed)


@pytest.mark.asyncio
async def test_a_domain_added_while_harvey_runs_is_skipped_next_cycle(state, tmp_path):
    """The case that was seen: discovered first, listed afterwards."""
    path = tmp_path / "harvey.yaml"
    shutil.copy(PROJECT_ROOT / "harvey.yaml", path)
    config = load_config(str(path))
    await _company(state, "acme.example")
    text = path.read_text().replace("  do_not_contact: []",
                                    '  do_not_contact: ["acme.example"]')
    assert text != path.read_text()
    path.write_text(text)

    scout = _scout(state, config)
    await scout._prospect_via_known_companies()
    assert scout.crawled == []


@pytest.mark.asyncio
async def test_a_subdomain_is_skipped_and_a_lookalike_is_not(state):
    await _company(state, "shop.acme.example")
    await _company(state, "notacme.example")
    scout = _scout(state, _config(["acme.example"]))
    await scout._prospect_via_known_companies()
    assert scout.crawled == ["notacme.example"]


@pytest.mark.asyncio
async def test_an_address_entry_does_not_skip_the_company(state):
    """"jane@acme.example" bars Jane, not her colleagues."""
    await _company(state, "acme.example")
    scout = _scout(state, _config(["jane@acme.example"]))
    await scout._prospect_via_known_companies()
    assert scout.crawled == ["acme.example"]


@pytest.mark.asyncio
async def test_hunter_spends_no_search_on_a_listed_company(state, monkeypatch):
    await _company(state, "acme.example")
    await _company(state, "other.example")
    searched = []

    async def people(domain, api_key):
        searched.append(domain)
        return []

    monkeypatch.setattr(ef, "hunter_domain_people", people)
    scout = Scout(brain=None, state=state, config=_config(["acme.example"]),
                  env=SimpleNamespace(hunter_api_key="test-key"))
    await scout._prospect_via_hunter()
    assert searched == ["other.example"]


@pytest.mark.asyncio
async def test_no_team_page_crawl_and_no_verification_for_a_listed_domain(state, monkeypatch):
    scout = Scout(brain=None, state=state, config=_config(["acme.example"]),
                  env=SimpleNamespace())

    async def scrape(domain):
        raise AssertionError(f"crawled {domain}")

    async def find_email(*a, **k):
        raise AssertionError("verified an address at a listed domain")

    scout._scrape_team_page = scrape
    monkeypatch.setattr(scout_mod, "find_email", find_email)
    assert await scout._contacts_from_company(
        company_id="c1", domain="acme.example", company_name="Acme", industry="Roofing",
    ) == []
    assert await scout._resolve_email("Jane", "Doe", "acme.example") == ("", "invalid")


@pytest.mark.asyncio
async def test_a_listed_published_inbox_is_never_verified(state, monkeypatch):
    scout = Scout(brain=None, state=state, config=_config(["info@acme.example"]),
                  env=SimpleNamespace())

    async def inbox(domain):
        return "info@acme.example"

    async def verify(*a, **k):
        raise AssertionError("verified a listed address")

    scout._find_inbox_address = inbox
    monkeypatch.setattr(scout_mod, "verify_published_address", verify)
    assert await scout._company_inbox_prospect("c1", "acme.example", "Acme", "Roofing") is None


def test_discovery_by_search_rejects_a_listed_domain(state):
    scout = Scout(brain=None, state=state, config=_config(["acme.example"]),
                  env=SimpleNamespace())
    assert not scout._is_candidate_domain("acme.example")
    assert not scout._is_candidate_domain("www.acme.example")
    assert scout._is_candidate_domain("notacme.example")


# ── Writer ──

WRITER_CONFIG = SimpleNamespace(
    channels=SimpleNamespace(email=SimpleNamespace(provider="instantly", require_approval=False)),
    product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
    persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    compliance=ComplianceConfig(do_not_contact=["acme.example"]),
)


async def _prospect(state, email):
    return await state.add_prospect(Prospect(
        first_name="Pat", last_name="Doe", title="Owner", company="Co",
        industry="Roofing", email=email, email_status="verified",
    ))


@pytest.mark.asyncio
async def test_the_writer_drafts_nothing_for_a_listed_prospect(state):
    pid = await _prospect(state, "pat@acme.example")
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, WRITER_CONFIG, None).run()
    assert brain.calls == 0
    assert (await state.get_prospect(pid)).status == DO_NOT_CONTACT_STATUS
    # Out of 'new', so the heartbeat moves on instead of writing again.
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, WRITER_CONFIG, summary=summary) == "prospect"


@pytest.mark.asyncio
async def test_the_writer_still_drafts_for_everyone_else(state):
    listed = await _prospect(state, "pat@acme.example")
    kept = await _prospect(state, "pat@other.example")
    await Writer(FakeBrain(reply=GOOD), state, WRITER_CONFIG, None).run()
    assert (await state.get_prospect(listed)).status == DO_NOT_CONTACT_STATUS
    assert (await state.get_prospect(kept)).status == "queued"
