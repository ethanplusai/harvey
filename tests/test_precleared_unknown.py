"""An ``unknown`` answer from Precleared holds the email instead of failing it.

Precleared answers ``unknown`` when a fact can't be established (usually the
recipient's country, sometimes a register that answered too slowly). The
email is moved back with a note saying what would settle it and asked about
again: after ADDRESS_RETRY_AFTER the first time, then after
UNKNOWN_RETRY_AFTER. The UNKNOWN_ATTEMPTS-th unknown answer on the same facts
fails it, as consent_needed and blocked do; a change to the facts starts the
count again, and a definite answer clears it. No network: every Precleared
call goes to an httpx.MockTransport.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from sellbird.agents.sender import ADDRESS_RETRY_AFTER
from sellbird.integrations.precleared import (
    SETTINGS_PREFIX,
    UNKNOWN_ATTEMPTS,
    UNKNOWN_PREFIX,
    UNKNOWN_RETRY_AFTER,
    PreclearedClient,
)
from sellbird.state import MOVED_BACK_PREFIXES, RECHECK_PREFIX, UNKNOWN_HOLD_PREFIX, StateManager
from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect
from tests.test_precleared import CONSENT_BODY, OK_BODY, mock_status, queue, with_check
from tests.test_precleared_pause import make_due_again, people, send_at

JANE = "jane@acme.example"
SAM = "sam@beta.example"

UNKNOWN_BODY = {
    "verdict": "unknown",
    "confidence": "low",
    "reasons": [{
        "rule_key": "xx_unknown_country",
        "text": "The recipient's country could not be determined from the caller, "
                "the domain or Companies House, so no rule can be applied.",
    }],
    "next_step": "Pass recipient_country, or default_recipient_country if all your "
                 "prospects are in one country (prospect_country in Sellbird), or a "
                 "company number, and check again. Until then treat as consent needed.",
    "receipt": {"id": "cc_rcpt_UN0000000001"},
}


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def answering(calls, bodies):
    """A Precleared stand-in answering each request with the next body (the
    last one repeats), recording the addresses asked about."""
    queue_ = list(bodies)

    def handler(request):
        calls.append(json.loads(request.content)["recipient_email"])
        body = queue_.pop(0) if len(queue_) > 1 else queue_[0]
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


async def the_item(state, email):
    return next(i for i in await state.get_outbox(limit=500) if i["to_email"] == email)


async def unknown_count(state, email):
    raw = await state.get_setting(UNKNOWN_PREFIX + email)
    return json.loads(raw)["attempts"] if raw else 0


def test_the_hold_is_short_and_the_cap_small():
    # Each unknown answer costs a check, so the count is small, and the
    # docs promise six hours, then a day.
    assert UNKNOWN_ATTEMPTS == 3
    assert ADDRESS_RETRY_AFTER == timedelta(hours=6)
    assert UNKNOWN_RETRY_AFTER == timedelta(hours=24)
    assert UNKNOWN_HOLD_PREFIX in MOVED_BACK_PREFIXES and RECHECK_PREFIX in MOVED_BACK_PREFIXES


@pytest.mark.asyncio
async def test_an_unknown_answer_holds_the_email_with_what_would_settle_it(state):
    await people(state, [JANE])
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), answering(calls, [UNKNOWN_BODY]))
    before = datetime.now(timezone.utc).replace(tzinfo=None)

    await sender._drain_due()

    assert provider.sent == []
    assert await state.get_outbox(status="failed") == []
    item = await the_item(state, JANE)
    assert item["status"] == "approved"
    assert item["error"].startswith(UNKNOWN_HOLD_PREFIX)
    assert "prospect_country in Sellbird" in item["error"]
    # Soon the first time, in case the problem was passing.
    assert before + ADDRESS_RETRY_AFTER - timedelta(minutes=1) <= await send_at(state, JANE)
    assert await send_at(state, JANE) <= before + ADDRESS_RETRY_AFTER + timedelta(minutes=1)
    # Nothing cached as a verdict; the count of unknown answers is one.
    assert await state.get_setting(SETTINGS_PREFIX + JANE) == ""
    assert await unknown_count(state, JANE) == 1
    assert calls == [JANE]


@pytest.mark.asyncio
async def test_a_held_email_is_not_asked_about_again_before_its_time(state):
    await people(state, [JANE])
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), answering(calls, [UNKNOWN_BODY]))

    await sender._drain_due()
    await sender._drain_due()

    assert calls == [JANE]  # the second cycle found nothing due
    assert await unknown_count(state, JANE) == 1


@pytest.mark.asyncio
async def test_the_second_hold_waits_a_day_and_the_third_unknown_fails_it(state):
    await people(state, [JANE])
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), answering(calls, [UNKNOWN_BODY]))

    await sender._drain_due()  # 1: held for six hours
    await make_due_again(state)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await sender._drain_due()  # 2: held for a day
    item = await the_item(state, JANE)
    assert item["status"] == "approved" and item["error"].startswith(UNKNOWN_HOLD_PREFIX)
    assert await send_at(state, JANE) >= before + UNKNOWN_RETRY_AFTER - timedelta(minutes=1)
    assert await unknown_count(state, JANE) == 2

    await make_due_again(state)
    await sender._drain_due()  # 3: the last unknown fails it

    assert provider.sent == []
    assert calls == [JANE] * UNKNOWN_ATTEMPTS
    failed = await state.get_outbox(status="failed")
    assert [i["to_email"] for i in failed] == [JANE]
    assert failed[0]["error"].startswith(f"precleared: unknown after {UNKNOWN_ATTEMPTS} checks:")
    assert "treat as consent needed" in failed[0]["error"]


@pytest.mark.asyncio
async def test_a_definite_answer_on_a_later_try_sends_and_clears_the_note(state):
    await people(state, [JANE])
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), answering(calls, [UNKNOWN_BODY, OK_BODY]))

    await sender._drain_due()
    await make_due_again(state)
    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == [JANE]
    sent = await state.get_outbox(status="sent")
    assert sent[0]["error"] == ""  # the hold note is gone
    assert calls == [JANE, JANE]
    # The verdict is cached and the unknown count is gone with it.
    assert await state.get_setting(SETTINGS_PREFIX + JANE) != ""
    assert await state.get_setting(UNKNOWN_PREFIX + JANE) == ""


@pytest.mark.asyncio
async def test_a_stopping_answer_on_a_later_try_fails_it_as_usual(state):
    await people(state, [JANE])
    calls = []
    sender = with_check(make_sender(state, FakeProvider()),
                        answering(calls, [UNKNOWN_BODY, CONSENT_BODY]))

    await sender._drain_due()
    await make_due_again(state)
    await sender._drain_due()

    failed = await state.get_outbox(status="failed")
    assert [i["to_email"] for i in failed] == [JANE]
    assert failed[0]["error"].startswith("precleared: consent_needed")
    assert await state.get_setting(UNKNOWN_PREFIX + JANE) == ""


@pytest.mark.asyncio
async def test_changed_facts_start_the_count_again(state):
    pid = await seed_prospect(state, email=JANE)
    await queue(state, pid, to=JANE)
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), answering(calls, [UNKNOWN_BODY]))

    await sender._drain_due()
    await make_due_again(state)
    await sender._drain_due()
    assert await unknown_count(state, JANE) == 2

    # The user sets prospect_country: a different question to Precleared.
    sender.precleared = PreclearedClient(sender_country="GB", prospect_country="US",
                                         transport=answering(calls, [UNKNOWN_BODY]))
    await make_due_again(state)
    await sender._drain_due()

    assert await unknown_count(state, JANE) == 1
    item = await the_item(state, JANE)
    assert item["status"] == "approved" and item["error"].startswith(UNKNOWN_HOLD_PREFIX)


@pytest.mark.asyncio
async def test_other_addresses_are_not_held_up_by_an_unknown(state):
    await people(state, [JANE, SAM])
    calls = []
    provider = FakeProvider()

    def answer(request):
        email = json.loads(request.content)["recipient_email"]
        calls.append(email)
        return httpx.Response(200, json=UNKNOWN_BODY if email == JANE else OK_BODY)
    sender = with_check(make_sender(state, provider), httpx.MockTransport(answer))

    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == [SAM]
    assert sorted(calls) == [JANE, SAM]
    assert (await the_item(state, JANE))["error"].startswith(UNKNOWN_HOLD_PREFIX)
    # An unknown is an answer, not an outage: no hold is recorded for the Today page.
    assert await state.get_setting("precleared_status") == ""


@pytest.mark.asyncio
async def test_rejecting_a_held_email_drops_the_note(state):
    await people(state, [JANE])
    sender = with_check(make_sender(state, FakeProvider()), mock_status(200, UNKNOWN_BODY))
    await sender._drain_due()
    item = await the_item(state, JANE)
    assert item["error"].startswith(UNKNOWN_HOLD_PREFIX)

    assert await state.reject_outbox_item(item["id"]) == 1

    rejected = await state.get_outbox_item(item["id"])
    assert rejected["status"] == "rejected" and rejected["error"] == ""


@pytest.mark.asyncio
async def test_an_old_count_starts_again(state):
    await people(state, [JANE])
    await state.set_setting(UNKNOWN_PREFIX + JANE, json.dumps({
        "inputs": PreclearedClient("GB")._fingerprint(), "attempts": 2,
        "at": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
    }))
    sender = with_check(make_sender(state, FakeProvider()), mock_status(200, UNKNOWN_BODY))

    await sender._drain_due()

    assert await unknown_count(state, JANE) == 1
    assert (await the_item(state, JANE))["status"] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({"inputs": "other", "attempts": 2})])
async def test_a_malformed_or_foreign_count_is_ignored(state, raw):
    await people(state, [JANE])
    await state.set_setting(UNKNOWN_PREFIX + JANE, raw)
    sender = with_check(make_sender(state, FakeProvider()), mock_status(200, UNKNOWN_BODY))

    await sender._drain_due()

    assert await unknown_count(state, JANE) == 1
