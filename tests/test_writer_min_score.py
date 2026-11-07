"""channels.email.min_write_score: the writer leaves low-scored prospects alone.

Scout scores every prospect 0-100 for ICP fit, but the writer used to draft
for anyone with a deliverable address, whatever the score. With a threshold
set, a prospect below it keeps its address and stays 'new' (no sequence, no
Claude call); 0, the default, writes for everyone as before.
"""

import logging
from types import SimpleNamespace

import pytest

from sellbird.agents.writer import Writer
from sellbird.models.prospect import Prospect
from tests.test_writer_retry import GOOD, FakeBrain, state, status  # noqa: F401  (state is a fixture)


def config(min_write_score=None):
    email = {"provider": "instantly", "require_approval": False}
    if min_write_score is not None:
        email["min_write_score"] = min_write_score
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(**email)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )


async def add(state, n, score):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        industry="Roofing", email=f"p{n}@co{n}.example", email_status="verified",
        score=score,
    ))


def test_the_option_defaults_to_zero():
    from sellbird.config import EmailChannelConfig
    assert EmailChannelConfig().min_write_score == 0
    assert EmailChannelConfig(min_write_score=60).min_write_score == 60


@pytest.mark.asyncio
async def test_prospects_below_the_threshold_are_left_new(state, caplog):
    low = await add(state, 1, 30)
    high = await add(state, 2, 80)
    brain = FakeBrain(reply=GOOD)
    caplog.set_level(logging.INFO, logger="sellbird.writer")
    await Writer(brain, state, config(min_write_score=60), None).run()
    assert await status(state, high) == "queued"
    assert await status(state, low) == "new"
    assert "Skipping 1 prospect(s) scored below channels.email.min_write_score (60)" in caplog.text


@pytest.mark.asyncio
async def test_nothing_above_the_threshold_means_no_model_call(state):
    low = await add(state, 1, 10)
    brain = FakeBrain(reply=GOOD)
    await Writer(brain, state, config(min_write_score=50), None).run()
    assert brain.calls == 0
    assert await status(state, low) == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [None, 0])
async def test_zero_or_unset_writes_for_everyone(state, threshold):
    low = await add(state, 1, 0)
    await Writer(FakeBrain(reply=GOOD), state, config(threshold), None).run()
    assert await status(state, low) == "queued"
