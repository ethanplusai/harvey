"""The Writer plans its model calls against the heartbeat's cycle bound.

A write cycle that runs out of clock is cancelled mid-draft by the cycle
bound (main.CYCLE_TIMEOUT_SECONDS): the sequence call in flight is wasted
and the next cycle pays for it again, halving write throughput on heavy
days — seen in production on two consecutive mornings (Oct 2026), where
every cycle with three or more campaigns to write died at the bound. Two
changes:

- Personal drafts run concurrently, bounded: a batch costs one sequence
  call plus a few waves of personal calls, not one model call per prospect
  in a row.
- run(deadline=...) won't START another batch once the time left wouldn't
  fit it — leftovers ride the next cycle, the same contract as the batch
  caps. The first batch always runs, so a cycle always makes progress, and
  the heartbeat's hard cancel stays what it was bought for: a hang guard,
  not a throughput cap.
"""

import asyncio
from time import monotonic
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.writer import (
    HUMANIZE_BUDGET_SECONDS,
    PERSONAL_BUDGET_SECONDS,
    PERSONAL_DRAFT_CONCURRENCY,
    SEQUENCE_BUDGET_SECONDS,
    Writer,
)
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.state import StateManager

PERSONAL = {"subject": "quick one", "body": "Hi, who answers the phones at Acme?"}
SEQUENCE = [
    {"step": 1, "subject": "hello there", "body": "Hi {{first_name}}, a question?",
     "delay_days": 0},
    {"step": 2, "subject": "following up", "body": "Another angle.", "delay_days": 3},
    {"step": 3, "subject": "closing", "body": "Last note from me.", "delay_days": 4},
]


class FakeBrain:
    """Serves both writer call shapes; tracks personal-call concurrency."""

    def __init__(self, personal=PERSONAL, sequence=SEQUENCE):
        self.personal = personal
        self.sequence = sequence
        self.sequence_calls = 0
        self.personal_calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.last_reply_empty = False

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        if "ONE cold email" not in prompt:
            self.sequence_calls += 1
            return self.sequence
        self.personal_calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Sleep so a batch's drafts really overlap: with no await here
            # the gather would run them one after another even unbounded.
            # Long enough to dwarf the per-draft aiosqlite thread spawns in
            # the fact-gathering that runs before each call.
            await asyncio.sleep(0.25)
            self.last_reply_empty = False
            return self.personal
        finally:
            self.in_flight -= 1


def config(provider="gmail"):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            provider=provider, require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "w.db"))
    await sm.init_db()
    return sm


async def add(state, n, industry="Roofing"):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        industry=industry, email=f"p{n}@co{n}.example", email_status="verified",
    ))


@pytest.mark.asyncio
async def test_personal_drafts_run_concurrently_but_bounded(state):
    for i in range(5):
        await add(state, i)  # one industry: one batch of five
    brain = FakeBrain()
    await Writer(brain, state, config(), None).run()

    assert brain.sequence_calls == 1
    assert brain.personal_calls == 5
    # Five independent drafts overlapped, but never more than the cap at once.
    assert brain.max_in_flight == PERSONAL_DRAFT_CONCURRENCY
    assert len(await state.get_outbox()) == 5


@pytest.mark.asyncio
async def test_a_batch_that_wont_fit_waits_for_the_next_cycle(state):
    first = await add(state, 1, industry="Roofing")
    second = await add(state, 2, industry="Dentistry")
    brain = FakeBrain()
    writer = Writer(brain, state, config(), None)
    # Room for (a bit less than) one singleton batch, not two.
    one_batch = writer._batch_estimate([object()])
    await writer.run(deadline=monotonic() + one_batch - 5)

    assert brain.sequence_calls == 1
    assert len(await state.get_campaigns_by_status("draft")) == 1
    statuses = {(await state.get_prospect(p)).status for p in (first, second)}
    assert statuses == {"new", "queued"}  # the second prospect was left alone

    # The next heartbeat, with a fresh cycle budget, picks up the rest.
    await Writer(brain, state, config(), None).run()
    assert brain.sequence_calls == 2
    assert (await state.get_prospect(second)).status == "queued"
    assert len(await state.get_outbox()) == 2


@pytest.mark.asyncio
async def test_the_first_batch_runs_even_when_nothing_fits(state):
    await add(state, 1, industry="Roofing")
    await add(state, 2, industry="Dentistry")
    brain = FakeBrain()
    writer = Writer(brain, state, config(), None)
    await writer.run(deadline=monotonic() - 1)  # already spent

    # Progress beats perfection: one batch still ran; only the second waited.
    assert brain.sequence_calls == 1
    assert len(await state.get_outbox()) == 1


@pytest.mark.asyncio
async def test_a_generous_deadline_writes_every_batch(state):
    await add(state, 1, industry="Roofing")
    await add(state, 2, industry="Dentistry")
    brain = FakeBrain()
    writer = Writer(brain, state, config(), None)
    await writer.run(deadline=monotonic() + 3600)

    assert brain.sequence_calls == 2
    assert len(await state.get_outbox()) == 2


@pytest.mark.asyncio
async def test_without_a_deadline_behavior_is_unchanged(state):
    await add(state, 1, industry="Roofing")
    await add(state, 2, industry="Dentistry")
    brain = FakeBrain()
    await Writer(brain, state, config(), None).run()

    assert brain.sequence_calls == 2
    assert len(await state.get_outbox()) == 2


@pytest.mark.asyncio
async def test_stranded_drafts_also_wait_for_a_cycle_that_can_fit_them(state):
    stranded = []
    for i in (1, 2):
        pid = await state.add_prospect(Prospect(
            first_name=f"S{i}", last_name="Doe", title="Owner", company=f"Side{i}",
            industry="Roofing", email=f"s{i}@side{i}.example", email_status="risky",
            status="queued",
        ))
        campaign = Campaign(
            id="", name=f"ops batch {i}", channel="email",
            sequence=[EmailStep(step=1, subject="hi {{first_name}}",
                                body="Template for {{company}}. Question?", delay_days=0)],
            prospect_ids=[pid], status="draft",
        )
        campaign.id = await state.add_campaign(campaign)
        stranded.append(pid)

    brain = FakeBrain()
    writer = Writer(brain, state, config(), None)
    await writer.run(deadline=monotonic() - 1)

    # The first stranded group was drafted; the second waits for a later cycle.
    assert brain.personal_calls == 1
    assert brain.sequence_calls == 0  # stranded drafts reuse the campaign
    assert len(await state.get_outbox()) == 1


def test_batch_estimate_charges_waves_and_the_humanizer_pass():
    writer = Writer(FakeBrain(), None, config(), None)
    three = [object()] * 3
    assert writer._batch_estimate(three) == (
        SEQUENCE_BUDGET_SECONDS + PERSONAL_BUDGET_SECONDS  # one wave
    )

    humanizer_on = config()
    humanizer_on.writer = SimpleNamespace(humanizer=True)
    writer = Writer(FakeBrain(), None, humanizer_on, None)
    assert writer._batch_estimate(three) == (
        SEQUENCE_BUDGET_SECONDS + PERSONAL_BUDGET_SECONDS
        + 3 * HUMANIZE_BUDGET_SECONDS
    )


def test_batch_estimate_on_a_legacy_provider_has_no_personal_calls():
    writer = Writer(FakeBrain(), None, config("instantly"), None)
    assert writer._batch_estimate([object()] * 8) == SEQUENCE_BUDGET_SECONDS
