"""Prospects the Writer can't use must not stop Harvey from prospecting.

The Writer only drafts for verified or risky addresses; a guessed, invalid or
missing email leaves the prospect 'new'. The heartbeat used to count every
'new' prospect, so a single guessed address made it choose write_campaign
every cycle, the Writer had nothing to do, and Scout never ran again.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.main import decide_next_action
from harvey.models.prospect import Prospect
from harvey.state import StateManager

CONFIG = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
    provider="gmail", require_approval=True)))


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "stall.db"))
    await state.init_db()
    return state


async def add(state, n, email, email_status):
    await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        email=email, email_status=email_status,
    ))


@pytest.mark.asyncio
async def test_summary_counts_only_prospects_the_writer_can_use(state):
    await add(state, 1, "a@one.test", "verified")
    await add(state, 2, "b@two.test", "risky")
    await add(state, 3, "c@three.test", "guess")
    await add(state, 4, "d@four.test", "invalid")
    await add(state, 5, "", "")
    summary = await state.get_state_summary()
    assert summary["prospects"]["new"] == 5
    assert summary["writable_prospects"] == 2


@pytest.mark.asyncio
async def test_guessed_emails_alone_lead_back_to_prospecting(state):
    for n in range(3):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "prospect"


async def age_all(state, days):
    """Pretend every prospect was found ``days`` ago."""
    import aiosqlite
    from datetime import datetime, timedelta, timezone
    then = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET created_at = ?", (then,))
        await db.commit()


@pytest.mark.asyncio
async def test_a_day_of_unusable_prospects_pauses_spending_and_says_why(state):
    # With no usable email at all (say, no verifier key), prospecting every
    # cycle would burn search and verifier credits on people Harvey can
    # never email. A day's worth pauses it, and status explains why.
    from harvey.main import PROSPECT_BACKLOG, unusable_backlog_notice
    for n in range(PROSPECT_BACKLOG):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "idle"
    notice = unusable_backlog_notice(summary)
    assert str(PROSPECT_BACKLOG) in notice and "verifier" in notice and "about a day" in notice


@pytest.mark.asyncio
async def test_the_pause_never_becomes_permanent(state):
    # Nothing re-checks a guessed address, so an all-time count would stop
    # Scout for good. Once the guesses are a day old, prospecting resumes.
    from harvey.main import PROSPECT_BACKLOG, unusable_backlog_notice
    for n in range(PROSPECT_BACKLOG + 10):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    await age_all(state, days=2)
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "prospect"
    assert unusable_backlog_notice(summary) == ""


@pytest.mark.asyncio
async def test_old_guesses_do_not_stop_a_mixed_setup_returning_to_prospecting(state):
    for n in range(25):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    await age_all(state, days=3)
    await add(state, 100, "v@verified.test", "verified")
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "write_campaign"
    [verified] = [p for p in await state.get_prospects_by_status("new") if p.email_status == "verified"]
    await state.update_prospect_status(verified.id, "queued")  # what the Writer does
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "prospect"


@pytest.mark.asyncio
async def test_no_backlog_notice_while_things_are_moving(state):
    from harvey.main import unusable_backlog_notice
    await add(state, 1, "p1@co1.test", "guess")
    assert unusable_backlog_notice(await state.get_state_summary()) == ""
    await add(state, 2, "p2@co2.test", "verified")
    for n in range(3, 30):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    assert unusable_backlog_notice(await state.get_state_summary()) == ""


@pytest.mark.asyncio
async def test_a_deliverable_prospect_is_still_written_for(state):
    await add(state, 1, "p1@co1.test", "guess")
    await add(state, 2, "p2@co2.test", "verified")
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "write_campaign"


@pytest.mark.asyncio
async def test_a_summary_without_the_new_count_keeps_the_old_rule():
    # Callers that build their own summary (older tests, scripts) still work.
    summary = {"prospects": {"new": 3}, "draft_campaigns": 0, "open_conversations": 0}
    assert await decide_next_action(None, None, CONFIG, summary=summary) == "write_campaign"


def email_config(**email):
    return SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
        **{"provider": "gmail", "require_approval": True, "enabled": True, **email})))


@pytest.mark.asyncio
async def test_drafts_with_the_email_channel_off_do_not_starve_prospecting():
    # The list-building setup: the Sender returns at once with email off, so
    # a draft stays a draft and choosing send_campaign for it never ends.
    summary = {"prospects": {"new": 0}, "draft_campaigns": 1, "open_conversations": 0}
    assert await decide_next_action(
        None, None, email_config(enabled=False), summary=summary) == "prospect"


@pytest.mark.asyncio
async def test_native_drafts_while_sending_is_paused_do_not_starve_prospecting():
    # The kill switch stops native staging, not finding or writing.
    summary = {"prospects": {"new": 0}, "draft_campaigns": 1,
               "open_conversations": 0, "sending_paused": True}
    assert await decide_next_action(None, None, email_config(), summary=summary) == "prospect"
    summary["prospects"] = {"new": 2}
    assert await decide_next_action(None, None, email_config(), summary=summary) == "write_campaign"
    summary["sending_paused"] = False
    assert await decide_next_action(None, None, email_config(), summary=summary) == "send_campaign"


@pytest.mark.asyncio
async def test_the_summary_reports_the_kill_switch(state):
    assert (await state.get_state_summary())["sending_paused"] is False
    await state.set_setting("sending_paused", "bounce rate 12%")
    assert (await state.get_state_summary())["sending_paused"] is True
