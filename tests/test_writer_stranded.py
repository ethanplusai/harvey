"""Prospects inserted straight to 'queued' still get drafted.

Rows can enter the table as 'queued' without ever being 'new' — an import,
an ops run — sometimes with a campaign already staged for them. The Writer
selected only status='new', so they sat queued forever with no personalized
first email and nothing in the Outbox to review, and the heartbeat's
writable count (also 'new'-only) never even chose write_campaign for them.

Now the Writer also picks up 'queued' prospects no draft exists for: ones
already in a campaign get their missing step-1 email staged into that
campaign (never a second campaign — that would double-send), the rest ride
the normal flow. The heartbeat counts the same set, so it can't starve Scout
by choosing write_campaign for prospects the Writer then ignores. On a
legacy provider the campaign template is the draft, so a queued prospect in
a campaign there is fully drafted. A personal draft that keeps coming back
unusable gives up after MAX_WRITE_ATTEMPTS tries and the template stands in.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.writer import Writer
from harvey.main import decide_next_action
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.state import (
    MAX_WRITE_ATTEMPTS,
    PERSONAL_ATTEMPTS_KEY,
    StateManager,
)

PERSONAL = {"subject": "quick one", "body": "Hi, who answers the phones at Acme?"}
SEQUENCE = [
    {"step": 1, "subject": "hello there", "body": "Hi {{first_name}}, a question?",
     "delay_days": 0},
    {"step": 2, "subject": "following up", "body": "Another angle.", "delay_days": 3},
]


class FakeBrain:
    """Serves the personal-email call; remembers every model call."""

    def __init__(self, personal=PERSONAL, sequence=SEQUENCE):
        self.personal = personal
        self.sequence = sequence
        self.calls = 0
        self.last_reply_empty = False

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.calls += 1
        self.last_reply_empty = False
        if "ONE cold email" in prompt:
            return self.personal
        return self.sequence


def config(provider):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            provider=provider, require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        humanizer=SimpleNamespace(enabled=False),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "w.db"))
    await sm.init_db()
    return sm


async def queue_directly(state, email="jane@acme.com", estatus="risky"):
    """A row an ops run might insert: never 'new', already 'queued'."""
    return await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="Owner", company="Acme",
        industry="Roofing", email=email, email_status=estatus, status="queued",
    ))


async def campaign_for(state, prospect_ids, name="ops batch"):
    campaign = Campaign(
        id="", name=name, channel="email",
        sequence=[
            EmailStep(step=1, subject="hi {{first_name}}",
                      body="Template for {{company}}. Question?", delay_days=0),
            EmailStep(step=2, subject="follow", body="Later.", delay_days=3),
        ],
        prospect_ids=prospect_ids, status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


@pytest.mark.asyncio
async def test_queued_in_a_campaign_gets_its_missing_first_email(state):
    pid = await queue_directly(state)
    campaign = await campaign_for(state, [pid])
    brain = FakeBrain()
    await Writer(brain, state, config("gmail"), None).run()

    [row] = await state.get_outbox()
    assert row["campaign_id"] == campaign.id and row["prospect_id"] == pid
    assert row["step"] == 1 and row["subject"] == PERSONAL["subject"]
    # No second campaign was written for them: that would double-send.
    assert len(await state.get_campaigns_by_status("draft")) == 1
    # And the heartbeat stops offering them: the draft exists now.
    assert await state.count_writable_prospects(native=True) == 0


@pytest.mark.asyncio
async def test_queued_with_no_campaign_rides_the_normal_flow(state):
    pid = await queue_directly(state, estatus="verified")
    brain = FakeBrain()
    await Writer(brain, state, config("gmail"), None).run()

    # One campaign written for them, with a personalized step 1 staged.
    [campaign] = await state.get_campaigns_by_status("draft")
    assert pid in campaign.prospect_ids
    [row] = await state.get_outbox()
    assert row["campaign_id"] == campaign.id and row["step"] == 1
    assert (await state.get_prospect(pid)).status == "queued"


@pytest.mark.asyncio
async def test_a_prospect_with_a_draft_is_left_alone(state):
    pid = await queue_directly(state)
    campaign = await campaign_for(state, [pid])
    await state.add_outbox_item(
        prospect_id=pid, campaign_id=campaign.id, step=1, to_email="jane@acme.com",
        subject="already written", body="Done.", send_at="2026-01-01T00:00:00",
    )
    brain = FakeBrain()
    await Writer(brain, state, config("gmail"), None).run()
    assert brain.calls == 0 and len(await state.get_outbox()) == 1


@pytest.mark.asyncio
async def test_unusable_personal_drafts_stop_after_three_tries(state):
    pid = await queue_directly(state)
    await campaign_for(state, [pid])
    brain = FakeBrain(personal="not a json object you can use")
    writer = Writer(brain, state, config("gmail"), None)
    for _ in range(MAX_WRITE_ATTEMPTS):
        await writer.run()
    assert await state.get_setting(PERSONAL_ATTEMPTS_KEY + pid) == str(MAX_WRITE_ATTEMPTS)

    calls = brain.calls
    await writer.run()  # no longer offered: the template stands in
    assert brain.calls == calls
    assert await state.get_stranded_queued_prospects(native=True) == []
    summary = await state.get_state_summary(native_email=True)
    assert await decide_next_action(None, state, config("gmail"), summary=summary) != (
        "write_campaign")


@pytest.mark.asyncio
async def test_an_outage_never_counts_as_a_bad_draft(state):
    pid = await queue_directly(state)
    await campaign_for(state, [pid])
    brain = FakeBrain(personal=None)

    async def empty_reply(prompt, **kw):
        brain.calls += 1
        brain.last_reply_empty = True
        return None

    brain.think_json = empty_reply
    writer = Writer(brain, state, config("gmail"), None)
    for _ in range(MAX_WRITE_ATTEMPTS + 2):
        await writer.run()
    # Still offered every cycle, and nothing was counted against them.
    assert await state.get_stranded_queued_prospects(native=True)
    assert await state.get_setting(PERSONAL_ATTEMPTS_KEY + pid) == ""


@pytest.mark.asyncio
async def test_success_clears_the_strike_count(state):
    pid = await queue_directly(state)
    await campaign_for(state, [pid])
    brain = FakeBrain(personal="unusable")
    writer = Writer(brain, state, config("gmail"), None)
    await writer.run()
    assert await state.get_setting(PERSONAL_ATTEMPTS_KEY + pid) == "1"
    brain.personal = PERSONAL
    await writer.run()
    [row] = await state.get_outbox()
    assert row["subject"] == PERSONAL["subject"]
    assert await state.get_setting(PERSONAL_ATTEMPTS_KEY + pid) == ""


@pytest.mark.asyncio
async def test_retry_writes_clears_the_strike_count_too(state):
    pid = await queue_directly(state)
    await campaign_for(state, [pid])
    brain = FakeBrain(personal="unusable")
    writer = Writer(brain, state, config("gmail"), None)
    for _ in range(MAX_WRITE_ATTEMPTS):
        await writer.run()
    assert await state.get_stranded_queued_prospects(native=True) == []
    await state.retry_write_failed()
    assert await state.get_stranded_queued_prospects(native=True)


@pytest.mark.asyncio
async def test_legacy_provider_counts_a_campaign_as_the_draft(state):
    # On Instantly the campaign template IS the draft: a queued prospect in
    # a campaign must not be offered again, or the Writer would ignore them
    # while the heartbeat kept choosing write_campaign (Scout starvation).
    pid = await queue_directly(state)
    await campaign_for(state, [pid])
    brain = FakeBrain()
    writer = Writer(brain, state, config("instantly"), None)
    await writer.run()
    assert brain.calls == 0
    assert await state.count_writable_prospects() == 0
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, config("instantly"), summary=summary) != (
        "write_campaign")


@pytest.mark.asyncio
async def test_legacy_provider_writes_a_campaign_for_the_campaign_less(state):
    # Queued with no campaign at all on a legacy provider: the normal flow
    # writes one, same as for a 'new' prospect.
    pid = await queue_directly(state, estatus="verified")
    brain = FakeBrain()
    await Writer(brain, state, config("instantly"), None).run()
    [campaign] = await state.get_campaigns_by_status("draft")
    assert pid in campaign.prospect_ids
    assert (await state.get_prospect(pid)).status == "queued"


@pytest.mark.asyncio
async def test_only_failed_campaigns_means_not_drafted(state):
    pid = await queue_directly(state)
    broken = await campaign_for(state, [pid], name="broken")
    await state.update_campaign(broken.id, status="failed")
    brain = FakeBrain()
    await Writer(brain, state, config("gmail"), None).run()
    # A failed campaign can't deploy: a fresh one is written for them.
    campaigns = await state.get_campaigns_by_status("draft")
    assert len(campaigns) == 1 and pid in campaigns[0].prospect_ids


@pytest.mark.asyncio
async def test_a_second_run_stages_no_second_draft(state):
    pid = await queue_directly(state)
    campaign = await campaign_for(state, [pid])
    await Writer(FakeBrain(), state, config("gmail"), None).run()
    # A second Writer run must not stage a second step-1 row.
    await Writer(FakeBrain(), state, config("gmail"), None).run()
    assert len(await state.get_outbox()) == 1
