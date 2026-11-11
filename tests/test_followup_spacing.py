"""A follow-up never goes out before, or too soon after, the email it follows.

Sequence emails are staged with send times days apart. If step 1 is held
(awaiting approval, sending paused, a compliance hold, a warmup ramp), the
follow-ups' scheduled times still arrive; without spacing, approving late
sends the whole sequence at once, or step 2 before step 1.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_campaign, seed_prospect, state,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_approving_a_week_late_sends_step_one_only(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])           # steps 1 and 2, 3 days apart
    sender = make_sender(state, FakeProvider())
    await sender._run_native()                   # staged, awaiting approval
    week_ago = _now() - timedelta(days=7)
    for item in await state.get_outbox(status="pending_review"):
        await state.update_outbox_item(
            item["id"], send_at=(week_ago + timedelta(days=3 * (item["step"] - 1))).isoformat())
    await state.approve_outbox()                 # "Approve all", a week late

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert [m["subject"] for m in provider.sent] == ["hi Jane"]   # step 1 only
    step2 = next(i for i in await state.get_outbox(status="approved") if i["step"] == 2)
    assert step2  # waits its 3 days after step 1 actually went out


@pytest.mark.asyncio
async def test_a_follow_up_whose_first_email_never_went_out_waits(state):
    pid = await seed_prospect(state, status="queued")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s1", body="One. Question?",
        send_at=(_now() + timedelta(days=1)).isoformat(), status="approved",
        campaign_id="c1", step=1)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2", body="Two. Question?",
        send_at=(_now() - timedelta(minutes=1)).isoformat(), status="approved",
        campaign_id="c1", step=2)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []


async def _sequence(state, pid, step, status, send_at, campaign="c1", to="jane@acme.com"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject=f"s{step}", body=f"Step {step}. Question?",
        send_at=send_at.isoformat(), status=status, campaign_id=campaign, step=step)


async def _sent(state, item_id, at):
    await state.update_outbox_item(item_id, status="sent", sent_at=at.isoformat())


@pytest.mark.asyncio
async def test_a_follow_up_goes_once_its_gap_has_passed(state):
    pid = await seed_prospect(state, status="contacted")
    one = await _sequence(state, pid, 1, "approved", _now() - timedelta(days=5))
    await _sent(state, one, _now() - timedelta(days=4))
    await _sequence(state, pid, 2, "approved", _now() - timedelta(days=2))
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert [m["subject"] for m in provider.sent] == ["s2"]


@pytest.mark.asyncio
async def test_a_waiting_follow_up_is_rescheduled_for_when_it_can_go(state):
    pid = await seed_prospect(state, status="contacted")
    one = await _sequence(state, pid, 1, "approved", _now() - timedelta(days=7))
    await _sent(state, one, _now() - timedelta(hours=1))
    two = await _sequence(state, pid, 2, "approved", _now() - timedelta(days=4))
    await make_sender(state, FakeProvider())._drain_due()
    send_at = datetime.fromisoformat((await state.get_outbox_item(two))["send_at"])
    assert send_at > _now() + timedelta(hours=20)  # out of the due queue until then


@pytest.mark.asyncio
async def test_the_campaign_delay_sets_the_gap_not_a_late_staging_time(state):
    # Step 2 was staged late (sending was paused), so its planned time is far
    # after step 1's: the campaign's 3 days still decide when it may go.
    pid = await seed_prospect(state, status="contacted")
    await seed_campaign(state, [pid])  # step 2 delay_days=3
    campaign = (await state.get_campaigns_by_status("draft"))[0]
    one = await _sequence(state, pid, 1, "approved", _now() - timedelta(days=20), campaign=campaign.id)
    await _sent(state, one, _now() - timedelta(days=4))
    await _sequence(state, pid, 2, "approved", _now() - timedelta(minutes=5), campaign=campaign.id)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert [m["subject"] for m in provider.sent] == ["s2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prev_status", ["rejected", "failed", "cancelled"])
async def test_a_follow_up_to_an_email_that_never_went_out_is_cancelled(state, prev_status):
    pid = await seed_prospect(state, status="contacted")
    await _sequence(state, pid, 1, prev_status, _now() - timedelta(days=5))
    two = await _sequence(state, pid, 2, "approved", _now() - timedelta(days=1))
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    item = await state.get_outbox_item(two)
    assert item["status"] == "cancelled" and "wasn't sent" in item["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["just sent", "still pending"])
async def test_a_page_of_waiting_follow_ups_never_holds_up_a_reply_or_new_email(state, first):
    # 30 follow-ups older than everything else, all waiting on their step 1.
    for n in range(30):
        pid = await seed_prospect(state, email=f"p{n}@acme.com", status="contacted")
        one = await _sequence(state, pid, 1,
                              "approved" if first == "just sent" else "pending_review",
                              _now() - timedelta(days=9), to=f"p{n}@acme.com")
        if first == "just sent":
            await _sent(state, one, _now() - timedelta(hours=1))
        await _sequence(state, pid, 2, "approved", _now() - timedelta(days=6), to=f"p{n}@acme.com")
    warm = await seed_prospect(state, email="bob@beta.com", status="replied")
    await state.add_outbox_item(
        prospect_id=warm, to_email="bob@beta.com", subject="Re: hi", body="Tuesday works.",
        send_at=_now().isoformat(), status="approved", kind="reply")
    fresh = await seed_prospect(state, email="ann@gamma.com", status="queued")
    await _sequence(state, fresh, 1, "approved", _now() - timedelta(minutes=1),
                    campaign="c2", to="ann@gamma.com")

    provider = FakeProvider()
    for _ in range(2):  # the first cycle may reschedule; nothing waits twice
        await make_sender(state, provider)._drain_due()
    assert {m["to"] for m in provider.sent} == {"bob@beta.com", "ann@gamma.com"}


@pytest.mark.asyncio
async def test_with_follow_ups_off_only_the_first_email_is_staged(state, monkeypatch):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])           # steps 1 and 2
    sender = make_sender(state, FakeProvider())
    # The test config's classes are shared between tests: restore the value after.
    monkeypatch.setattr(sender.config.channels.email, "follow_ups", False, raising=False)
    await sender._run_native()
    steps = [i["step"] for i in await state.get_outbox(status="pending_review")]
    assert steps == [1]


@pytest.mark.asyncio
async def test_follow_ups_are_staged_unless_turned_off(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    await make_sender(state, FakeProvider())._run_native()
    steps = sorted(i["step"] for i in await state.get_outbox(status="pending_review"))
    assert steps == [1, 2]
