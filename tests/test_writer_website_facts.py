"""The Writer sees what profiling and discovery found on a company's site.

Before this, a first email could only lean on the company's description,
its tech stack and hiring signals: findings like "no online booking" or a
Google Maps position showed on the dashboard card but never reached the
prompt, so the draft opened with something generic instead.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.signals import seed_signal_catalog
from sellbird.state import StateManager
from sellbird.website_facts import website_facts


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "w.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    return s


async def company(state, n=1):
    return await state.add_company(Company(name="Oak Ridge Tree Care", domain=f"oakridge{n}.example"))


async def observe(state, cid, code, num=None, text="", when=None, prospect_id=""):
    await state.add_observation(code, company_id=cid, prospect_id=prospect_id, collector="profile",
                                value_num=num, value_text=text, observed_at=when)


@pytest.mark.asyncio
async def test_findings_become_fact_lines(state):
    cid = await company(state)
    await observe(state, cid, "NO_ONLINE_BOOKING", 1)
    await observe(state, cid, "RUNNING_GOOGLE_ADS", 0)
    await observe(state, cid, "MAPS_RANK", 7, "tree removal Springfield, IL")
    await observe(state, cid, "BLOG_STALE", 210, "newest content 2026-04-10")
    await observe(state, cid, "REVIEW_RATING", 4.8)
    await observe(state, cid, "REVIEW_COUNT", 41)

    facts = "\n".join(await website_facts(state, cid))
    assert "Their website: no online booking or scheduling found" in facts
    assert "Their website: not running Google Ads" in facts
    assert ("Google: Google Maps position: #7 for 'tree removal Springfield, IL'" in facts
            and "below the three businesses Google shows first" in facts)
    assert "newest content 2026-04-10 on their site (about 7 months ago" in facts
    assert "Google: Google rating 4.8 from 41 reviews" in facts


@pytest.mark.asyncio
async def test_booking_found_is_stated_not_left_out(state):
    """A missing line must never read as a finding: having booking is said."""
    cid = await company(state)
    await observe(state, cid, "NO_ONLINE_BOOKING", 0)
    facts = await website_facts(state, cid)
    assert facts == ["- Their website: has online booking or scheduling on their homepage"]


@pytest.mark.asyncio
async def test_newest_observation_wins(state):
    cid = await company(state)
    await observe(state, cid, "NO_ONLINE_BOOKING", 1, when="2026-01-01 10:00:00")
    await observe(state, cid, "NO_ONLINE_BOOKING", 0, when="2026-06-01 10:00:00")
    facts = "\n".join(await website_facts(state, cid))
    assert "has online booking" in facts and "no online booking" not in facts


@pytest.mark.asyncio
async def test_ranks_are_kept_per_phrase_best_first_and_lost_ones_dropped(state):
    cid = await company(state)
    await observe(state, cid, "MAPS_RANK", 12, "arborist Springfield, IL", when="2026-06-01 10:00:00")
    await observe(state, cid, "MAPS_RANK", 2, "tree trimming Springfield, IL", when="2026-06-01 10:00:01")
    await observe(state, cid, "MAPS_RANK", 9, "stump grinding Springfield, IL", when="2026-01-01 10:00:00")
    await observe(state, cid, "MAPS_RANK", 0, "stump grinding Springfield, IL", when="2026-06-01 10:00:02")
    facts = await website_facts(state, cid)
    assert facts == [
        "- Google: Google Maps position: #2 for 'tree trimming Springfield, IL', "
        "searched from the city centre (in the top three)",
        "- Google: Google Maps position: #12 for 'arborist Springfield, IL', "
        "searched from the city centre (below the three businesses Google shows first)",
    ]


@pytest.mark.asyncio
async def test_person_level_findings_and_unknown_companies_add_nothing(state):
    cid = await company(state)
    await observe(state, cid, "LIKELY_OWNER", 1, "Dana Reyes", prospect_id="p1")
    assert await website_facts(state, cid) == []
    assert await website_facts(state, "") == []
    assert await website_facts(None, cid) == []


@pytest.mark.asyncio
async def test_a_failed_read_is_nothing_known():
    class Broken:
        async def get_observations(self, **kw):
            raise RuntimeError("database is locked")
    assert await website_facts(Broken(), "c1") == []


class RecordingBrain:
    def __init__(self):
        self.prompts = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "PROMPT"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "estimates on your site", "body": "Hi Dana"}


def writer(state, brain):
    from sellbird.agents.writer import Writer
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )
    w = Writer(brain, state, cfg)
    w.skills = ""
    return w


@pytest.mark.asyncio
async def test_the_personal_email_prompt_carries_the_findings(state):
    cid = await company(state)
    await observe(state, cid, "NO_ONLINE_BOOKING", 1)
    await observe(state, cid, "MAPS_RANK", 6, "tree service Springfield, IL")
    pid = await state.add_prospect(Prospect(
        first_name="Dana", last_name="Reyes", title="Owner", company="Oak Ridge Tree Care",
        company_id=cid, email="dana@oakridge1.example", email_status="verified"))
    brain = RecordingBrain()
    await writer(state, brain)._write_personal_email(await state.get_prospect(pid))
    prompt = brain.prompts[0]
    assert "- Their website: no online booking or scheduling found" in prompt
    assert "Google Maps position: #6 for 'tree service Springfield, IL'" in prompt
