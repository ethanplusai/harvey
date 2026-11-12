"""channels.email.max_sends_per_cycle: how many approved emails leave per
heartbeat cycle. Default 8 (the old constant); 1 spreads a small daily cap
over the day instead of sending it all in one minute."""

import pytest

from sellbird.agents import sender as sender_mod
from tests.test_outbox_native import (  # noqa: F401  (state fixture)
    FakeProvider, _now_iso, make_sender, seed_prospect, state,
)

pytestmark = pytest.mark.asyncio


class _Email:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def test_sends_per_cycle_defaults_and_floors():
    assert sender_mod.sends_per_cycle(_Email()) == sender_mod.MAX_SENDS_PER_CYCLE
    assert sender_mod.sends_per_cycle(_Email(max_sends_per_cycle=3)) == 3
    assert sender_mod.sends_per_cycle(_Email(max_sends_per_cycle=0)) == 1
    assert sender_mod.sends_per_cycle(_Email(max_sends_per_cycle="x")) == sender_mod.MAX_SENDS_PER_CYCLE


async def _three_approved(state):
    for i in range(3):
        pid = await seed_prospect(state, email=f"owner{i}@example{i}.com")
        await state.add_outbox_item(
            prospect_id=pid, to_email=f"owner{i}@example{i}.com",
            subject="hello", body="Plain body. Question?",
            send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
        )


async def test_one_per_cycle_leaves_the_rest_approved(state):
    await _three_approved(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    email = sender.config.channels.email   # a shared test class: restore it after
    email.max_sends_per_cycle = 1
    try:
        await sender._drain_due()
        assert len(provider.sent) == 1
        assert len(await state.get_outbox(status="approved")) == 2

        await sender._drain_due()   # the next cycle sends the next one
        assert len(provider.sent) == 2
        assert len(await state.get_outbox(status="approved")) == 1
    finally:
        del email.max_sends_per_cycle


async def test_default_sends_them_all_in_one_cycle(state):
    await _three_approved(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)   # its config has no such setting

    await sender._drain_due()
    assert len(provider.sent) == 3
    assert await state.get_outbox(status="approved") == []


async def test_blocked_emails_at_the_front_do_not_use_up_the_cycle(state):
    """Three risky addresses queued ahead of a verified one, one send a
    cycle: the gate fails the three and the verified one still goes."""
    for i in range(3):
        pid = await seed_prospect(state, email=f"risky{i}@example{i}.com", estatus="risky")
        await state.add_outbox_item(
            prospect_id=pid, to_email=f"risky{i}@example{i}.com",
            subject="hello", body="Plain body. Question?",
            send_at=f"2026-01-01T00:00:0{i}", status="approved", campaign_id="c1", step=1,
        )
    pid = await seed_prospect(state, email="owner@example.com")
    await state.add_outbox_item(
        prospect_id=pid, to_email="owner@example.com",
        subject="hello", body="Plain body. Question?",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    email = sender.config.channels.email
    email.max_sends_per_cycle = 1
    try:
        await sender._drain_due()
    finally:
        del email.max_sends_per_cycle
    assert [m["to"] for m in provider.sent] == ["owner@example.com"]
    assert len(await state.get_outbox(status="failed")) == 3
    assert await state.get_outbox(status="approved") == []
