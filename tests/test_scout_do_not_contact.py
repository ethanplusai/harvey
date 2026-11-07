"""Scout leaves companies on compliance.do_not_contact alone.

The send gate already refuses mail to them, but by then Scout had read the
company's site, found a person and spent a verification credit on their
address. Now every strategy asks first, with the gate's own domain matcher:
the backfill passes mark the company checked (so the rotation moves on) and
skip it; discovery drops it before any row is written.
"""

from types import SimpleNamespace

import pytest

from sellbird.agents.scout import Scout
from sellbird.config import ComplianceConfig

LISTED = "acme.example"
OTHER = "lantern.example"


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

    async def add_prospect(self, prospect):
        return "p1"

    async def prospect_exists(self, **kw):
        return False


def make_scout(companies, do_not_contact=(LISTED,)):
    config = SimpleNamespace(
        icp=SimpleNamespace(titles=["Owner"], industries=["Roofing"], geography=[]),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)),
        compliance=ComplianceConfig(do_not_contact=list(do_not_contact)),
    )
    env = SimpleNamespace(hunter_api_key="")
    scout = Scout(brain=None, state=FakeState(companies), config=config, env=env)
    scout._seen_domains = set()
    scout._seen_prospect_keys = set()
    scout.contacted = []

    async def contacts(company_id, domain, **kw):
        scout.contacted.append(domain)
        return []

    scout._contacts_from_company = contacts
    return scout


COMPANIES = [
    {"id": "c1", "domain": LISTED, "name": "Acme", "industry": "Roofing"},
    {"id": "c2", "domain": "mail." + LISTED, "name": "Acme Mail", "industry": "Roofing"},
    {"id": "c3", "domain": OTHER, "name": "Lantern", "industry": "Roofing"},
]


@pytest.mark.asyncio
async def test_backfill_skips_listed_companies_and_marks_them_checked():
    scout = make_scout(COMPANIES)
    await scout._prospect_via_known_companies()
    assert scout.contacted == [OTHER]
    # Listed ones are checked too, so the rotation moves past them.
    assert scout.state.checked == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_hunter_backfill_spends_no_credit_on_a_listed_company(monkeypatch):
    scout = make_scout(COMPANIES[:1] + COMPANIES[2:])
    scout.env = SimpleNamespace(hunter_api_key="fake-key")
    looked_up = []

    async def people(domain, api_key, limit=10):
        looked_up.append(domain)
        return []

    import sellbird.agents.scout as scout_mod
    import sellbird.integrations.email_finder as ef
    monkeypatch.setattr(ef, "hunter_domain_people", people)
    monkeypatch.setattr(ef, "_hunter_monthly_cap", lambda: 25)
    # One domain a cycle by default; two here so the pass reaches the
    # company after the listed one.
    monkeypatch.setattr(scout_mod, "HUNTER_DOMAINS_PER_CYCLE", 2)
    await scout._prospect_via_hunter()
    assert looked_up == [OTHER]
    assert scout.state.checked == ["c1", "c3"]


@pytest.mark.asyncio
async def test_discovery_drops_a_listed_company_before_it_becomes_a_row():
    scout = make_scout([])
    ensured = []

    async def search():
        return [{"domain": LISTED, "name": "Acme"}, {"domain": OTHER, "name": "Lantern"}]

    async def no_directories():
        return []

    async def ensure(name, domain, **kw):
        ensured.append(domain)
        return "cid-" + domain

    async def no_signals(*a, **kw):
        return ""

    scout._find_companies_via_search = search
    scout._find_companies_via_directories = no_directories
    scout._ensure_company = ensure
    scout._enrich_company_signals = no_signals
    await scout._prospect_via_company_discovery()
    assert ensured == [OTHER]
    assert scout.contacted == [OTHER]


@pytest.mark.asyncio
async def test_an_address_entry_does_not_bar_the_whole_company():
    # "jane@acme.example" bars one mailbox; the gate matcher leaves the
    # company discoverable, and so does Scout.
    scout = make_scout(COMPANIES[:1], do_not_contact=("jane@" + LISTED,))
    await scout._prospect_via_known_companies()
    assert scout.contacted == [LISTED]


@pytest.mark.asyncio
async def test_an_empty_list_changes_nothing():
    scout = make_scout(COMPANIES, do_not_contact=())
    await scout._prospect_via_known_companies()
    assert scout.contacted == [LISTED, "mail." + LISTED, OTHER]
