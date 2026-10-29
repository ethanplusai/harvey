"""Prospects that can never be emailed are retired, not parked in 'new'.

A prospect saved with no address and no company row has no domain: no
email pattern can be learned and a verifier has nothing to check, so no
later stage can move it out of 'new' — found as 32 orphan rows inflating
every 'new' count. After a grace period the heartbeat retires them to
'undeliverable'; rows that could still gain an address stay.
"""

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from harvey.models.prospect import Prospect
from harvey.state import StateManager, UNDELIVERABLE_AFTER_DAYS


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "retire.db"))
    await state.init_db()
    return state


async def add_orphan(state, name, *, status="new", email="", company_id=""):
    """A LinkedIn-style hit: a person with a title, maybe nothing else."""
    prospect = Prospect(
        first_name=name, last_name="Doe", title="Founder",
        linkedin_url=f"https://www.linkedin.com/in/{name.lower()}-example",
        company=f"{name} Industries", company_id=company_id,
        email=email, status=status, source="web_search",
    )
    return await state.add_prospect(prospect)


async def age(state, prospect_id, days):
    """Pretend a prospect was found ``days`` ago."""
    then = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET created_at = ? WHERE id = ?", (then, prospect_id))
        await db.commit()


async def status_of(state, prospect_id):
    return (await state.get_prospect(prospect_id)).status


@pytest.mark.asyncio
async def test_old_orphans_are_retired_with_an_audit_row(state):
    orphan = await add_orphan(state, "Old")
    await age(state, orphan, UNDELIVERABLE_AFTER_DAYS + 1)
    retired = await state.retire_undeliverable_prospects()
    assert retired == 1
    assert await status_of(state, orphan) == "undeliverable"
    async with aiosqlite.connect(state.db_path) as db:
        row = await db.execute_fetchall(
            "SELECT action_type, details_json FROM actions ORDER BY created_at DESC LIMIT 1"
        )
    assert row and row[0][0] == "retired_undeliverable"
    assert '"retired": 1' in row[0][1]


@pytest.mark.asyncio
async def test_fresh_orphans_get_the_grace_period(state):
    orphan = await add_orphan(state, "Fresh")
    retired = await state.retire_undeliverable_prospects()
    assert retired == 0
    assert await status_of(state, orphan) == "new"


@pytest.mark.asyncio
async def test_company_rows_and_address_rows_are_left_alone(state):
    # No address yet, but a company to learn a pattern from: recoverable.
    linked = await add_orphan(state, "Linked", company_id="co_1")
    # An address exists (even an unusable one): not the retirement rule's call.
    guessed = await add_orphan(state, "Guessed", email="g@example.test")
    await age(state, linked, UNDELIVERABLE_AFTER_DAYS + 1)
    await age(state, guessed, UNDELIVERABLE_AFTER_DAYS + 1)
    assert await state.retire_undeliverable_prospects() == 0
    assert await status_of(state, linked) == "new"
    assert await status_of(state, guessed) == "new"


@pytest.mark.asyncio
async def test_only_new_rows_are_swept(state):
    # An orphan that already moved on (say, queued by hand) is not pulled back.
    queued = await add_orphan(state, "Queued", status="queued")
    await age(state, queued, UNDELIVERABLE_AFTER_DAYS + 1)
    assert await state.retire_undeliverable_prospects() == 0
    assert await status_of(state, queued) == "queued"


@pytest.mark.asyncio
async def test_the_sweep_is_idempotent(state):
    orphan = await add_orphan(state, "Once")
    await age(state, orphan, UNDELIVERABLE_AFTER_DAYS + 1)
    assert await state.retire_undeliverable_prospects() == 1
    assert await state.retire_undeliverable_prospects() == 0
    assert await status_of(state, orphan) == "undeliverable"


@pytest.mark.asyncio
async def test_retired_rows_leave_the_new_count(state):
    orphan = await add_orphan(state, "Counted")
    keeper = await add_orphan(state, "Keeper", email="k@example.test", company_id="co_2")
    await age(state, orphan, UNDELIVERABLE_AFTER_DAYS + 1)
    await age(state, keeper, UNDELIVERABLE_AFTER_DAYS + 1)
    await state.retire_undeliverable_prospects()
    summary = await state.get_state_summary()
    assert summary["prospects"]["new"] == 1
    assert summary["prospects"]["undeliverable"] == 1
