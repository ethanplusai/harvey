"""Sellbird never emails someone the user already emailed by hand.

Marking a prospect sent from the dashboard leaves a durable marker. Whatever
Sellbird staged before, during or after that (the Writer's personal draft, the
Sender's template staging, a mailbox connected later) must not go out.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sellbird.state import MANUAL_SEND_KEY
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_campaign, seed_prospect, state,
)


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def _approved(state, pid, step, campaign_id="c1"):
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello there",
        body="A short, plain note. Question?", send_at=_now_iso(),
        status="approved", campaign_id=campaign_id, step=step,
    )


@pytest.mark.asyncio
async def test_drain_never_sends_to_someone_emailed_by_hand(state):
    pid = await seed_prospect(state, status="contacted")
    await state.set_setting(MANUAL_SEND_KEY + pid, "queued")
    await _approved(state, pid, step=1)
    await _approved(state, pid, step=2)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    # The same reason undo restores, so an undo brings these back too.
    assert {i["error"] for i in await state.get_outbox(status="cancelled")} == {"sent_by_hand"}


@pytest.mark.asyncio
async def test_drain_never_sends_a_first_email_to_someone_already_contacted(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid, step=1)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    assert (await state.get_outbox(status="cancelled"))[0]["error"] == "already contacted"


@pytest.mark.asyncio
async def test_follow_ups_to_someone_sellbird_contacted_still_go(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid, step=2)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_staging_never_overwrites_contacted_with_queued(state, monkeypatch):
    pid = await seed_prospect(state, status="queued")
    await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider())
    real_add = state.add_outbox_item

    async def add_then_user_marks_sent(**kw):
        item = await real_add(**kw)
        await state.update_prospect_status(pid, "contacted")  # mid-staging
        return item
    monkeypatch.setattr(state, "add_outbox_item", add_then_user_marks_sent)
    await sender._run_native()
    assert (await state.get_prospect(pid)).status == "contacted"


@pytest.mark.asyncio
async def test_restore_reschedules_from_now_keeping_the_gaps(state):
    pid = await seed_prospect(state, status="queued")
    old = datetime(2026, 1, 1)
    for step, days in ((1, 0), (2, 3)):
        await state.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
            send_at=(old + timedelta(days=days)).isoformat(), status="approved",
            campaign_id="c1", step=step)
    await state.cancel_pending_outbox_for_prospect(pid, reason="sent_by_hand")
    assert await state.restore_hand_cancelled_outbox(pid, reason="sent_by_hand") == 2
    items = sorted(await state.get_outbox(status="pending_review"), key=lambda i: i["step"])
    first, second = (datetime.fromisoformat(i["send_at"]) for i in items)
    assert first >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    assert second - first == timedelta(days=3)
