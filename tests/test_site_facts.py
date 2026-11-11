"""site_facts: what the profile collector read off a site reaches the writer.

The homepage signals were collected and shown in cohorts, but the writer's
prompt never carried them, so an email that should name one concrete thing
had nothing true to name. Companies and people here are invented.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.agents.writer import Writer
from sellbird.config import ComplianceConfig
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.signals import seed_signal_catalog
from sellbird.site_facts import HEADER, STALE_DAYS, THIN_PAGES, site_facts
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "s.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    return s


async def company_with(state, **signals):
    """A company with the given {signal_code: value_num} observed."""
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    for code, value in signals.items():
        await state.add_observation(
            company_id=cid, signal_code=code, collector="profile", value_num=value)
    return cid


# ── which findings become facts ──


@pytest.mark.asyncio
async def test_a_missing_schema_is_a_fact(state):
    cid = await company_with(state, NO_SCHEMA_MARKUP=1)
    facts = await site_facts(state, cid)
    assert facts[0] == HEADER
    assert len(facts) == 2 and "no JSON-LD structured data" in facts[1]


@pytest.mark.asyncio
async def test_a_signal_checked_and_found_fine_is_not_a_fact(state):
    cid = await company_with(
        state, NO_SCHEMA_MARKUP=0, BLOCKS_AI_CRAWLERS=0)
    assert await site_facts(state, cid) == []


@pytest.mark.asyncio
async def test_blocked_crawlers_are_a_fact(state):
    cid = await company_with(state, BLOCKS_AI_CRAWLERS=1)
    assert "blocks AI crawlers" in "\n".join(await site_facts(state, cid))


@pytest.mark.asyncio
async def test_no_online_booking_is_never_passed_on(state):
    # A homepage read misses booking widgets that load late, so this signal
    # stays out of the writer's facts however it was observed.
    cid = await company_with(state, NO_ONLINE_BOOKING=1)
    assert await site_facts(state, cid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("age, shown", [
    (STALE_DAYS - 1, False), (STALE_DAYS, True), (900, True)])
async def test_a_site_counts_as_stale_only_after_months(state, age, shown):
    cid = await company_with(state, BLOG_STALE=age)
    assert bool(await site_facts(state, cid)) is shown


@pytest.mark.asyncio
@pytest.mark.parametrize("pages, shown", [
    (1, True), (THIN_PAGES, True), (THIN_PAGES + 1, False), (0, False)])
async def test_only_a_very_small_site_is_called_small(state, pages, shown):
    cid = await company_with(state, SITE_PAGE_COUNT=pages)
    assert bool(await site_facts(state, cid)) is shown


@pytest.mark.asyncio
async def test_the_newest_observation_wins(state):
    cid = await company_with(state)
    await state.add_observation(
        company_id=cid, signal_code="NO_SCHEMA_MARKUP", collector="profile",
        value_num=1, observed_at="2026-01-01 00:00:00")
    await state.add_observation(
        company_id=cid, signal_code="NO_SCHEMA_MARKUP", collector="profile",
        value_num=0, observed_at="2026-02-01 00:00:00")
    assert await site_facts(state, cid) == []


@pytest.mark.asyncio
async def test_every_line_says_it_can_be_wrong(state):
    cid = await company_with(state, NO_SCHEMA_MARKUP=1)
    assert "can miss things" in (await site_facts(state, cid))[0]
    assert "never call it an audit" in HEADER


# ── nothing known, or a failed read, is never a reason not to write ──


@pytest.mark.asyncio
async def test_unknown_company_or_no_state_gives_nothing(state):
    assert await site_facts(state, "") == []
    assert await site_facts(None, "abc") == []
    assert await site_facts(state, "no-such-company") == []


@pytest.mark.asyncio
async def test_a_failed_read_gives_nothing():
    class Broken:
        async def get_observations(self, **kw):
            raise RuntimeError("db locked")

    assert await site_facts(Broken(), "abc") == []


# ── it reaches the writer's prompt ──


class RecordingBrain:
    def __init__(self):
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "hi", "body": "Hi"}


def writer(brain, state):
    config = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        writer=SimpleNamespace(humanizer=False, email_rules=""),
        compliance=ComplianceConfig(),
    )
    w = Writer(brain, state, config)
    w.skills = ""
    w.email_rules = ""
    return w


async def prompt_for(state, company_id):
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="Owner", company="Acme",
        industry="Bakery", email="ana@acme.example", email_status="verified",
        company_id=company_id))
    brain = RecordingBrain()
    await writer(brain, state)._write_personal_email(await state.get_prospect(pid))
    return brain.prompts[0]


@pytest.mark.asyncio
async def test_the_findings_are_in_the_personal_email_prompt(state):
    cid = await company_with(state, NO_SCHEMA_MARKUP=1, BLOCKS_AI_CRAWLERS=1)
    prompt = await prompt_for(state, cid)
    assert "Site checks" in prompt
    assert "no JSON-LD structured data" in prompt
    assert "blocks AI crawlers" in prompt


@pytest.mark.asyncio
async def test_a_company_with_no_findings_adds_no_section(state):
    cid = await company_with(state, NO_SCHEMA_MARKUP=0)
    assert "Site checks" not in await prompt_for(state, cid)


# ── writer.site_facts turns it off ──


def test_the_setting_is_on_by_default():
    from sellbird.config import WriterConfig
    assert WriterConfig().site_facts is True
    assert WriterConfig(site_facts=False).site_facts is False


@pytest.mark.asyncio
async def test_switched_off_the_writer_never_sees_the_findings(state):
    cid = await company_with(state, NO_SCHEMA_MARKUP=1)
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="Owner", company="Acme",
        industry="Bakery", email="ana@acme.example", email_status="verified",
        company_id=cid))
    prospect = await state.get_prospect(pid)
    for on, shown in ((True, True), (False, False)):
        brain = RecordingBrain()
        w = writer(brain, state)
        w.config.writer.site_facts = on
        await w._write_personal_email(prospect)
        assert ("Site checks" in brain.prompts[0]) is shown


@pytest.mark.asyncio
async def test_a_config_without_the_setting_keeps_it_on(state):
    cid = await company_with(state, NO_SCHEMA_MARKUP=1)
    assert "Site checks" in await prompt_for(state, cid)


# ── where the company is ──


@pytest.mark.asyncio
async def test_the_company_location_reaches_the_prompt(state):
    cid = await state.add_company(
        Company(name="Acme", domain="acme.example", location="Exampleville, Testland"))
    assert "- Location: Exampleville, Testland" in await prompt_for(state, cid)
