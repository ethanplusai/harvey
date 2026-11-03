"""What a failed Precleared check holds back, and what still goes out.

A check that fails because of Precleared itself (no connection, a busy
server, the daily allowance, the rate limit, the key, an answer Sellbird can't
read) pauses fresh checks for the rest of the cycle, but a stored verdict is
still used, so email to people already checked carries on. A failure that
may be about one address moves just that email back and the rest carry on;
so does an address Precleared got no usable answer for, though that also
pauses the cycle. The hold counts only email waiting on the check.
No network: every Precleared call goes to an httpx.MockTransport.
"""

import asyncio
import json
import os
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from sellbird.integrations.precleared import (
    BUY_URL,
    STATUS_KEY,
    PreclearedClient,
    hold_message,
)
from sellbird.state import RECHECK_PREFIX, StateManager
from sellbird.agents.sender import ADDRESS_RETRY_AFTER, DOMAINS_FAILING_TO_PAUSE
from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect
from tests.test_send_window import WED_0200, approved, pin_now, seed_at, window_sender
from tests.test_precleared import (
    CONSENT_BODY,
    OK_BODY,
    mock_precleared,
    mock_raw,
    mock_status,
    queue,
    with_check,
)

JANE = "jane@acme.example"
SAM = "sam@beta.example"
ANN = "ann@gamma.example"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


async def people(state, emails):
    for email in emails:
        pid = await seed_prospect(state, email=email)
        await queue(state, pid, to=email)


async def store(state, email, body, country="", company="", name=""):
    """Give an address a stored verdict, as an earlier cycle would have."""
    await PreclearedClient("GB", transport=mock_status(200, body)).clear(
        email, state, country, company=company, name=name)


def recording(calls, answer):
    """A Precleared stand-in that records each address and answers with answer(email)."""
    def handler(request):
        email = json.loads(request.content)["recipient_email"]
        calls.append(email)
        return answer(email)
    return httpx.MockTransport(handler)


async def stored_hold(state):
    return json.loads(await state.get_setting(STATUS_KEY) or "{}")


async def send_at(state, email):
    item = next(i for i in await state.get_outbox(limit=500) if i["to_email"] == email)
    return datetime.fromisoformat(item["send_at"])


async def make_due_again(state):
    """Let time pass until moved-back emails are due again."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    for item in await state.get_outbox(status="approved", limit=500):
        await state.update_outbox_item(item["id"], send_at=past)


# ── stored verdicts still count once checks are paused ──


@pytest.mark.asyncio
async def test_people_already_cleared_still_get_email_after_the_allowance_runs_out(state):
    await people(state, [JANE, SAM])
    await store(state, SAM, OK_BODY)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, status=429))

    await sender._drain_due()

    assert len(calls) == 1  # Jane's check met the used-up allowance
    assert [m["to"] for m in provider.sent] == [SAM]
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == [JANE]
    hold = await stored_hold(state)
    assert hold["kind"] == "keyless_limit" and hold["waiting"] == 1


@pytest.mark.asyncio
async def test_a_stored_stopping_verdict_still_stops_the_email_after_a_pause(state):
    await people(state, [JANE, SAM])
    await store(state, SAM, CONSENT_BODY, company="Acme", name="Jane Doe")
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], status=429))

    await sender._drain_due()

    assert provider.sent == []
    failed = await state.get_outbox(status="failed")
    assert [i["to_email"] for i in failed] == [SAM]
    assert failed[0]["error"].startswith("precleared: consent_needed")


@pytest.mark.asyncio
async def test_new_addresses_wait_once_checks_are_paused(state):
    await people(state, [JANE, SAM, ANN])
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), mock_precleared(calls, status=429))

    await sender._drain_due()

    assert len(calls) == 1
    assert (await stored_hold(state))["waiting"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_country,sent,waiting", [("FR", True, 1), ("GB", False, 2)])
async def test_paused_checks_use_each_recipients_country(state, cached_country, sent, waiting):
    await people(state, [JANE])
    pid = await seed_at(state, "Paris, France", SAM)
    await queue(state, pid, to=SAM)
    await store(state, SAM, OK_BODY, country=cached_country)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, status=429))
    await sender._drain_due()
    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == ([SAM] if sent else [])
    assert (await stored_hold(state))["waiting"] == waiting


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_country,waiting", [("FR", 1), ("GB", 2)])
async def test_waiting_count_validates_country_even_for_items_outside_page(state, monkeypatch,
                                                                         cached_country, waiting):
    await people(state, [JANE])
    pid = await seed_at(state, "Paris, France", SAM)
    await queue(state, pid, to=SAM)
    await store(state, SAM, OK_BODY, country=cached_country)
    sender = with_check(make_sender(state, FakeProvider()), mock_status(429, {}))
    real_get = state.get_outbox

    async def first_page_only(*args, **kwargs):
        rows = await real_get(*args, **kwargs)
        return [row for row in rows if row["to_email"] == JANE]

    monkeypatch.setattr(state, "get_outbox", first_page_only)
    await sender._drain_due()
    assert (await stored_hold(state))["waiting"] == waiting


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_name,sent,waiting", [("Jane Doe", True, 1), ("Former Person", False, 2)])
async def test_paused_checks_validate_recipient_names(state, cached_name, sent, waiting):
    await people(state, [JANE, SAM])
    await store(state, SAM, OK_BODY, company="Acme", name=cached_name)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, status=429))

    await sender._drain_due()

    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == ([SAM] if sent else [])
    assert (await stored_hold(state))["waiting"] == waiting


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_name,waiting", [("Jane Doe", 1), ("Former Person", 2)])
async def test_waiting_count_validates_names_outside_page(state, monkeypatch, cached_name, waiting):
    await people(state, [JANE, SAM])
    await store(state, SAM, OK_BODY, company="Acme", name=cached_name)
    sender = with_check(make_sender(state, FakeProvider()), mock_status(429, {}))
    real_get = state.get_outbox

    async def first_page_only(*args, **kwargs):
        return [row for row in await real_get(*args, **kwargs) if row["to_email"] == JANE]

    monkeypatch.setattr(state, "get_outbox", first_page_only)
    await sender._drain_due()
    assert (await stored_hold(state))["waiting"] == waiting


# ── a failure about one address moves only that email back ──


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 422, 500])
async def test_an_address_precleared_cant_check_is_moved_back_and_the_rest_go(state, status):
    refused = "refused@delta.example"
    await people(state, [refused, SAM])
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), recording(calls, lambda email: (
        httpx.Response(status, json={"error": "invalid_request"}) if email == refused
        else httpx.Response(200, json=OK_BODY))))

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await sender._drain_due()

    assert calls == [refused, SAM]
    assert [m["to"] for m in provider.sent] == [SAM]
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == [refused]
    assert await send_at(state, refused) >= before + ADDRESS_RETRY_AFTER
    # Precleared answered, so nothing says it's held up.
    assert await state.get_setting(STATUS_KEY) == ""

    await sender._drain_due()  # the next cycle doesn't ask about it again
    assert calls == [refused, SAM]
    await make_due_again(state)
    await sender._drain_due()  # once it's due again, it is
    assert calls == [refused, SAM, refused]


@pytest.mark.asyncio
async def test_two_failing_addresses_at_one_domain_dont_hold_up_the_others(state):
    bad = ["ann@broken.example", "bob@broken.example"]
    good = ["cat@fine.example", "dan@fine.test"]
    await people(state, bad + good)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), recording(calls, lambda email: (
        httpx.Response(500, json={"error": "server_error"}) if email.endswith("@broken.example")
        else httpx.Response(200, json=OK_BODY))))

    await sender._drain_due()

    # One domain failing twice isn't Precleared failing: no pause, no hold.
    assert calls == bad + good
    assert sorted(m["to"] for m in provider.sent) == sorted(good)
    assert await state.get_setting(STATUS_KEY) == ""
    for _ in range(2):
        await sender._drain_due()
    assert calls == bad + good  # moved back, so not asked again for now


def test_the_retry_wait_matches_the_docs():
    # README and CHANGELOG say six hours, and two domains pause checks.
    assert ADDRESS_RETRY_AFTER == timedelta(hours=6)
    assert DOMAINS_FAILING_TO_PAUSE == 2


@pytest.mark.asyncio
async def test_failures_at_two_domains_in_a_row_pause_checks_and_say_so(state):
    # A failure on Precleared's side that isn't a 502/503/504 (a bug that
    # answers 500 to everyone, a firewall's 403) still stops the checking.
    emails = [f"p{n}@c{n}.example" for n in range(12)]
    await people(state, emails)
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), recording(
        calls, lambda email: httpx.Response(500, content=b"Internal Server Error")))

    await sender._drain_due()

    assert calls == emails[:2]
    hold = await stored_hold(state)
    assert hold["kind"] == "unavailable" and "HTTP 500" in hold["reason"]
    assert hold["waiting"] == 12
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    moved = [e for e in emails if await send_at(state, e) > now]
    assert moved == emails[:2]  # the rest keep their place


@pytest.mark.asyncio
async def test_an_answer_between_two_failures_keeps_checks_going(state):
    bad = {"a@one.example", "c@three.example"}
    order = ["a@one.example", "b@two.example", "c@three.example", "d@four.example"]
    await people(state, order)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), recording(calls, lambda email: (
        httpx.Response(500, text="server error") if email in bad
        else httpx.Response(200, json=OK_BODY))))

    await sender._drain_due()

    assert calls == order
    assert [m["to"] for m in provider.sent] == ["b@two.example", "d@four.example"]
    assert await state.get_setting(STATUS_KEY) == ""


@pytest.mark.asyncio
async def test_a_stored_verdict_between_two_failures_does_not_reset_the_count(state):
    await people(state, ["a@one.example", SAM, "c@three.example", "d@four.example"])
    await store(state, SAM, OK_BODY)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), recording(
        calls, lambda email: httpx.Response(500, text="server error")))

    await sender._drain_due()

    # Sam goes on his stored verdict, which says nothing about Precleared
    # now, so the second failure still pauses and d@four.example isn't asked.
    assert calls == ["a@one.example", "c@three.example"]
    assert [m["to"] for m in provider.sent] == [SAM]
    assert (await stored_hold(state))["kind"] == "unavailable"


@pytest.mark.asyncio
async def test_checking_stops_once_the_cycles_checking_time_is_used(state, monkeypatch):
    import sellbird.agents.sender as sender_module

    monkeypatch.setattr(sender_module, "CHECK_SECONDS_PER_CYCLE", 0.05)
    await people(state, [JANE, SAM])
    reply = await seed_prospect(state, email=ANN, status="replied")
    await queue(state, reply, kind="reply", to=ANN)
    calls = []

    async def slow(request):
        calls.append(1)
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=CONSENT_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(slow))
    await sender._drain_due()

    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == [ANN]
    assert await state.get_setting(STATUS_KEY) == ""  # not a failure, so no hold


@pytest.mark.asyncio
async def test_an_address_in_two_campaigns_is_asked_once_a_cycle(state):
    refused = "refused@delta.example"
    pid = await seed_prospect(state, email=refused)
    await queue(state, pid, to=refused, campaign="c1")
    await queue(state, pid, to=refused, campaign="c2")
    await people(state, [SAM])
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), recording(calls, lambda email: (
        httpx.Response(500, text="server error") if email == refused
        else httpx.Response(200, json=OK_BODY))))

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await sender._drain_due()

    assert calls == [refused, SAM]
    assert [m["to"] for m in provider.sent] == [SAM]
    moved = [i for i in await state.get_outbox(status="approved") if i["to_email"] == refused]
    assert len(moved) == 2
    assert all(datetime.fromisoformat(i["send_at"]) >= before + ADDRESS_RETRY_AFTER for i in moved)


# ── failures that aren't about the address pause checks ──


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,api_key", [
    (429, {"error": "daily_limit"}, ""),
    (429, {"error": "rate_limited", "retry_after": 20}, "pc_live_test"),
    (402, {"error": "payment_required"}, "pc_live_test"),
    (401, {"error": "invalid_key"}, "pc_live_test"),
], ids=["daily-limit", "rate-limit", "out-of-checks", "rejected-key"])
async def test_a_failure_not_about_the_address_stops_checks_for_the_cycle(
        state, status, body, api_key):
    await people(state, [JANE, SAM])
    calls = []
    sender = with_check(make_sender(state, FakeProvider()),
                        recording(calls, lambda email: httpx.Response(status, json=body)),
                        api_key=api_key)

    await sender._drain_due()

    assert calls == [JANE]
    assert len(await state.get_outbox(status="approved")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    b"not json",
    json.dumps([]).encode(),
    json.dumps({"verdict": "ok_with_conditions"}).encode(),
    b"[" * 100_000 + b"]" * 100_000,
], ids=["not-json", "not-an-object", "unknown-verdict", "deeply-nested"])
async def test_an_answer_sellbird_cant_read_stops_checks_for_the_cycle(state, content):
    await people(state, [JANE, SAM])
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=content)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(handler))
    await sender._drain_due()

    assert len(calls) == 1
    assert provider.sent == []
    assert len(await state.get_outbox(status="approved")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_a_busy_server_stops_checks_for_the_cycle(state, status):
    await people(state, [JANE, SAM])
    calls = []
    sender = with_check(make_sender(state, FakeProvider()), recording(
        calls, lambda email: httpx.Response(status, text="upstream busy")))

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await sender._drain_due()

    assert calls == [JANE]
    hold = await stored_hold(state)
    assert hold["kind"] == "unavailable" and hold["waiting"] == 2
    # Both keep their place: the server, not the address, was the problem.
    assert await send_at(state, JANE) <= before
    assert await send_at(state, SAM) <= before


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    httpx.ConnectError("no route to host"),
    httpx.ReadError("connection reset"),
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
], ids=["no-connection", "reset", "dropped"])
async def test_a_network_error_stops_checks_for_the_cycle(state, error):
    await people(state, [JANE, SAM])
    calls = []

    def boom(request):
        calls.append(1)
        raise error

    sender = with_check(make_sender(state, FakeProvider()), httpx.MockTransport(boom))
    await sender._drain_due()

    assert len(calls) == 1
    # A connection that fails or drops says nothing about the address.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert all(datetime.fromisoformat(i["send_at"]) <= now
               for i in await state.get_outbox(status="approved"))


@pytest.mark.asyncio
async def test_a_key_that_cant_be_sent_holds_cold_email_and_replies_still_go(state):
    await people(state, [JANE])
    reply = await seed_prospect(state, email=ANN, status="replied")
    await queue(state, reply, kind="reply", to=ANN)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_status(200, OK_BODY),
                        api_key="pc_live_\u201ctest\u201d")

    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == [ANN]
    hold = await stored_hold(state)
    assert hold["kind"] == "key_rejected"
    assert "test" not in hold["reason"]


@pytest.mark.asyncio
async def test_an_unexpected_error_holds_cold_email_and_replies_still_go(state):
    await people(state, [JANE, SAM])
    reply = await seed_prospect(state, email=ANN, status="replied")
    await queue(state, reply, kind="reply", to=ANN)
    calls = []

    def broken(request):
        calls.append(1)
        raise RuntimeError("something nobody planned for, near pc_live_secret123")

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(broken),
                        api_key="pc_live_secret123")
    await sender._drain_due()

    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == [ANN]
    hold = await stored_hold(state)
    assert "RuntimeError" in hold["reason"] and "pc_live_secret123" not in hold["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["pc_live_abc\ndef", "pc_live_abc\x00def", "pc_live_abc def"],
                         ids=["line-break", "nul", "space"])
async def test_a_key_with_a_control_character_or_space_is_never_sent(state, key):
    calls = []
    client = PreclearedClient("GB", api_key=key, transport=mock_precleared(calls, OK_BODY))

    result = await client.clear(JANE, state)

    assert calls == []
    assert result.held and result.pause_checks and result.hold_kind == "key_rejected"
    assert "abc" not in result.reason


@pytest.mark.asyncio
async def test_a_character_that_cant_be_sent_elsewhere_isnt_blamed_on_the_key(state):
    client = PreclearedClient("GB", api_key="pc_live_test", pitch="Route planning \ud800",
                              transport=mock_status(200, OK_BODY))

    result = await client.clear(JANE, state)

    assert result.held and result.pause_checks
    assert result.hold_kind == "unavailable"
    assert "PRECLEARED_API_KEY" not in result.reason


@pytest.mark.asyncio
async def test_a_long_unexpected_error_still_hides_the_key(state):
    key = "pc_live_secret123"

    def broken(request):
        # The key straddles the 240-character limit of the reason.
        raise RuntimeError("x" * 190 + key)

    client = PreclearedClient("GB", api_key=key, transport=httpx.MockTransport(broken))
    result = await client.clear(JANE, state)

    assert result.held
    assert "pc_live" not in result.reason


@pytest.mark.asyncio
async def test_follow_ups_behind_a_page_of_waiting_first_emails_still_go(state):
    # Twenty-four new addresses wait behind a used-up allowance, ahead of
    # Sam's second email; Sam was cleared before, so his email still goes.
    await people(state, [f"new{n}@example.com" for n in range(24)])
    sam = await seed_prospect(state, email=SAM)
    await store(state, SAM, OK_BODY)
    first = await queue(state, sam, to=SAM, step=1)
    await state.update_outbox_item(first, status="sent",
                                   sent_at=(datetime.now(timezone.utc) - timedelta(days=5))
                                   .replace(tzinfo=None).isoformat())
    await queue(state, sam, to=SAM, step=2)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, status=429))

    await sender._drain_due()

    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == [SAM]


@pytest.mark.asyncio
async def test_an_answer_that_never_finishes_holds_cold_email(state, monkeypatch):
    import sellbird.integrations.precleared as precleared

    monkeypatch.setattr(precleared, "TOTAL_TIMEOUT", 0.05)

    async def trickle(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json=OK_BODY)

    result = await PreclearedClient("GB", transport=httpx.MockTransport(trickle)).clear(JANE, state)

    assert result.held and not result.allowed and result.pause_checks
    assert result.retry_address  # a slow lookup for this address could be why
    assert "too long" in result.reason


# ── a failure that may be down to the address, though it pauses checks ──


def raising(exc):
    def handler(request):
        raise exc
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    lambda request: httpx.Response(200, json={"verdict": "ok_with_conditions"}),
    lambda request: httpx.Response(200, content=b"not json"),
    lambda request: httpx.Response(200, json=[]),
    raising(httpx.ReadTimeout("no answer")),
    raising(RuntimeError("something nobody planned for")),
], ids=["unknown-verdict", "not-json", "not-an-object", "read-timeout", "unexpected-error"])
async def test_one_address_with_no_usable_answer_doesnt_keep_the_others_waiting(state, answer):
    stuck = "stuck@delta.example"
    await people(state, [stuck, JANE, SAM])
    calls = []

    def handler(request):
        email = json.loads(request.content)["recipient_email"]
        calls.append(email)
        return answer(request) if email == stuck else httpx.Response(200, json=OK_BODY)

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(handler))

    await sender._drain_due()  # first in line: checks pause and its email moves back
    assert calls == [stuck]
    assert provider.sent == []
    assert (await stored_hold(state))["waiting"] == 3

    await sender._drain_due()  # so the others are asked next cycle
    assert calls == [stuck, JANE, SAM]
    assert [m["to"] for m in provider.sent] == [JANE, SAM]
    assert await state.get_setting(STATUS_KEY) == ""


@pytest.mark.asyncio
async def test_the_pause_remembers_failures_from_earlier_cycles(state):
    # One new address a cycle, and Precleared answers 500 to everyone: the
    # second failure, a cycle later, still pauses checks and says so.
    calls = []
    failing = recording(calls, lambda email: httpx.Response(500, text="server error"))
    sender = with_check(make_sender(state, FakeProvider()), failing)

    await people(state, ["a@one.example"])
    await sender._drain_due()
    assert await state.get_setting(STATUS_KEY) == ""  # one failure: maybe the address

    await people(state, ["b@two.example"])
    await sender._drain_due()
    hold = await stored_hold(state)
    assert hold["kind"] == "unavailable" and hold["waiting"] == 2

    # An answer ends it, and starts the count again.
    with_check(sender, recording(calls, lambda email: httpx.Response(200, json=OK_BODY)))
    await people(state, ["c@three.example"])
    await sender._drain_due()
    assert await state.get_setting(STATUS_KEY) == ""
    with_check(sender, failing)
    await people(state, ["d@four.example"])
    await sender._drain_due()
    assert await state.get_setting(STATUS_KEY) == ""
    assert calls == ["a@one.example", "b@two.example", "c@three.example", "d@four.example"]


@pytest.mark.asyncio
async def test_an_address_failing_again_doesnt_pause_checks_again(state):
    # Two addresses Precleared refuses, failing in different cycles, look
    # like trouble at Precleared once. Their retries six hours later say
    # nothing new, so they don't hold up a new address or the hold.
    refused = ["a@one.example", "b@two.example"]
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), recording(calls, lambda email: (
        httpx.Response(400, json={"error": "invalid_request"}) if email in refused
        else httpx.Response(200, json=OK_BODY))))
    for email in refused:
        await people(state, [email])
        await sender._drain_due()
    assert (await stored_hold(state))["kind"] == "unavailable"
    await state.set_setting(STATUS_KEY, "")

    await make_due_again(state)
    await people(state, [JANE])
    await sender._drain_due()

    assert calls == refused + refused + [JANE]
    assert [m["to"] for m in provider.sent] == [JANE]
    assert await state.get_setting(STATUS_KEY) == ""


@pytest.mark.asyncio
async def test_an_address_failing_again_doesnt_count_towards_a_pause(state):
    refused = "a@one.example"
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), recording(calls, lambda email: (
        httpx.Response(400, json={"error": "invalid_request"})
        if email in (refused, "d@four.example") else httpx.Response(200, json=OK_BODY))))
    await people(state, [refused])
    await sender._drain_due()
    await people(state, [JANE])
    await sender._drain_due()  # an answer: the count starts again

    await make_due_again(state)
    await people(state, ["d@four.example", SAM])
    await sender._drain_due()

    # The retry and one new failure aren't two domains failing: Sam is asked.
    assert calls == [refused, JANE, refused, "d@four.example", SAM]
    assert [m["to"] for m in provider.sent] == [JANE, SAM]
    assert await state.get_setting(STATUS_KEY) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,wakes", [(OK_BODY, True), (CONSENT_BODY, False)],
                         ids=["cleared", "consent-needed"])
async def test_quiet_hours_still_wake_for_someone_cleared_after_a_hold(state, monkeypatch,
                                                                         verdict, wakes):
    new = await seed_at(state, "Tokyo, Japan", "aiko@tokyo.example")
    await approved(state, new, "aiko@tokyo.example")
    sender = window_sender(state, FakeProvider(), monkeypatch)
    with_check(sender, httpx.MockTransport(lambda request: httpx.Response(503)))
    pin_now(monkeypatch, WED_0200)  # 11:00 in Tokyo
    await sender._drain_due()
    assert not await sender.cold_due_in_window()  # the new address waits

    known = await seed_at(state, "Tokyo, Japan", "kenji@osaka.example")
    await store(state, "kenji@osaka.example", verdict, country="JP",
                company="osaka.example", name="Jane Doe")
    await approved(state, known, "kenji@osaka.example", step=2)

    assert await sender.cold_due_in_window() is wakes


# ── how many emails the hold says are waiting ──


@pytest.mark.asyncio
async def test_quiet_hours_find_cleared_follow_up_beyond_first_page(state, monkeypatch):
    import sellbird.agents.sender as sender_module

    monkeypatch.setattr(sender_module, "QUIET_HOURS_SCAN", 2)
    for email in (JANE, ANN):
        pid = await seed_at(state, "Tokyo, Japan", email)
        await approved(state, pid, email)
    known = await seed_at(state, "Tokyo, Japan", SAM)
    await approved(state, known, SAM, step=2)
    await store(state, SAM, OK_BODY, country="JP", company="beta.example", name="Jane Doe")
    sender = window_sender(state, FakeProvider(), monkeypatch)
    with_check(sender, httpx.MockTransport(lambda request: httpx.Response(503)))
    sender.compliance_held = True
    pin_now(monkeypatch, WED_0200)

    assert await sender.cold_due_in_window()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [
    {"verdict": "unknown"}, {"verdict": None}, {"checked_at": "not-a-date"},
])
async def test_waiting_count_matches_cache_rejection(state, corruption):
    await people(state, [JANE])
    await store(state, JANE, OK_BODY)
    key = "precleared:" + JANE
    entry = json.loads(await state.get_setting(key))
    entry.update(corruption)
    await state.set_setting(key, json.dumps(entry))
    client = PreclearedClient("GB")
    assert await client.cached(JANE, state) is None
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert await state.count_due_cold_outbox(now, stored_verdicts=client.stored_verdicts()) == 1


@pytest.mark.asyncio
async def test_the_waiting_count_leaves_out_email_to_people_already_cleared(state):
    # One new address and twelve people already cleared; the allowance is
    # used up. The cleared go out as far as the cycle allows and the rest
    # wait for the next cycle, not for the check: one email waits on it.
    await people(state, [JANE])
    cleared = [f"p{n}@c{n}.example" for n in range(12)]
    await people(state, cleared)
    for email in cleared:
        await store(state, email, OK_BODY)
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], status=429))

    await sender._drain_due()

    assert 0 < len(provider.sent) < len(cleared)
    assert (await stored_hold(state))["waiting"] == 1


@pytest.mark.asyncio
async def test_the_waiting_count_keeps_email_the_check_moved_back(state):
    emails = [f"p{n}@c{n}.example" for n in range(6)]
    await people(state, emails)
    sender = with_check(make_sender(state, FakeProvider()), mock_raw(500, b"server error"))

    for _ in range(3):
        await sender._drain_due()
        assert (await stored_hold(state))["waiting"] == 6


@pytest.mark.asyncio
async def test_the_count_of_email_waiting_on_the_check(state):
    client = PreclearedClient("GB", transport=mock_status(200, OK_BODY))
    names = ("fresh", "other", "old", "bad", "new", "moved", "cleared-since", "later")
    address = {name: f"{name}@acme.example" for name in names}
    await people(state, list(address.values()) + ["Mixed@Beta.example"])
    for name in ("fresh", "old", "cleared-since"):
        await client.clear(address[name], state)
    await client.clear("mixed@beta.example", state)  # stored lower-cased
    await PreclearedClient("US", transport=mock_status(200, OK_BODY)).clear(address["other"], state)
    key = "precleared:" + address["old"]
    entry = json.loads(await state.get_setting(key))
    entry["checked_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    await state.set_setting(key, json.dumps(entry))
    await state.set_setting("precleared:" + address["bad"], "not json")
    items = {i["to_email"]: i["id"] for i in await state.get_outbox(limit=50)}
    for name in ("moved", "cleared-since"):
        await state.update_outbox_item(items[address[name]], send_at="2999-01-01T00:00:00",
                                       error=RECHECK_PREFIX + "2999-01-01 00:00 UTC: test")
    await state.update_outbox_item(items[address["later"]], send_at="2999-01-01T00:00:00")
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    waiting = await state.count_due_cold_outbox(
        now, stored_verdicts=client.stored_verdicts(), moved_back_prefix=RECHECK_PREFIX)

    # other facts, expired, unreadable, none at all, and moved back
    assert waiting == 5
    assert await state.count_due_cold_outbox(now) == 6  # every due cold email


# ── what a moved-back email says ──


@pytest.mark.asyncio
async def test_a_moved_back_email_says_why_until_it_is_sent(state):
    refused = "refused@delta.example"
    await people(state, [refused])
    answer = {"status": 422}
    sender = with_check(make_sender(state, FakeProvider()), recording([], lambda email: (
        httpx.Response(answer["status"], json={"error": "invalid_request"})
        if answer["status"] != 200 else httpx.Response(200, json=OK_BODY))))

    await sender._drain_due()
    item = (await state.get_outbox(status="approved"))[0]
    assert item["error"].startswith(RECHECK_PREFIX) and "HTTP 422" in item["error"]

    answer["status"] = 200
    await make_due_again(state)
    await sender._drain_due()
    sent = await state.get_outbox(status="sent")
    assert [i["to_email"] for i in sent] == [refused]
    assert sent[0]["error"] == ""


@pytest.mark.asyncio
async def test_rejecting_a_moved_back_email_drops_the_note(state):
    await people(state, [JANE])
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(422, {"error": "invalid_request"}))
    await sender._drain_due()
    item = (await state.get_outbox(status="approved"))[0]
    assert item["error"].startswith(RECHECK_PREFIX)

    assert await state.reject_outbox_item(item["id"]) == 1

    rejected = await state.get_outbox_item(item["id"])
    assert rejected["status"] == "rejected" and rejected["error"] == ""


@pytest.mark.asyncio
async def test_control_characters_in_a_verdict_never_reach_the_outbox(state):
    body = json.loads(json.dumps(CONSENT_BODY))
    body["reasons"][0]["text"] = "Consent needed \x1b]8;;https://evil.example\x07here\x1b]8;;\x07"
    body["next_step"] = "Get consent\x1b[2J first.\x9b31m"
    await people(state, [JANE])
    sender = with_check(make_sender(state, FakeProvider()), mock_status(200, body))

    await sender._drain_due()

    failed = await state.get_outbox(status="failed")
    assert failed and failed[0]["error"].startswith("precleared: consent_needed")
    assert not any(unicodedata.category(ch) == "Cc" for ch in failed[0]["error"])


# ── what the reason says ──


@pytest.mark.asyncio
@pytest.mark.parametrize("api_key", ["pc_live_test", ""], ids=["with-key", "without-key"])
async def test_a_rate_limit_is_not_reported_as_a_used_up_allowance(state, api_key):
    client = PreclearedClient("GB", api_key=api_key, transport=mock_status(429, {
        "error": "rate_limited", "message": "Slow down.", "retry_after": 20,
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and result.pause_checks
    assert result.hold_kind == "rate_limited"
    assert "rate limit" in result.reason
    assert "add PRECLEARED_API_KEY" not in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    json.dumps({"error": "daily_limit"}).encode(), b"Too many requests",
], ids=["daily-limit-body", "plain-text"])
async def test_a_429_on_a_key_is_a_rate_limit_whatever_its_body(state, content):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_raw(429, content))

    result = await client.clear("person@example.com", state)

    assert result.hold_kind == "rate_limited"
    assert "add PRECLEARED_API_KEY" not in result.reason


@pytest.mark.asyncio
async def test_a_deeply_nested_keyless_429_is_held_not_raised(state):
    client = PreclearedClient("GB", transport=mock_raw(429, b"[" * 100_000 + b"]" * 100_000))

    result = await client.clear("person@example.com", state)

    assert result.held and result.pause_checks
    assert result.hold_kind == "keyless_limit"


@pytest.mark.asyncio
async def test_a_rate_limit_is_recorded_as_its_own_hold(state):
    await people(state, [JANE, SAM])
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(429, {"error": "rate_limited", "retry_after": 20}),
                        api_key="pc_live_test")

    await sender._drain_due()

    hold = await stored_hold(state)
    assert hold["kind"] == "rate_limited" and hold["waiting"] == 2
    assert hold_message(hold)["title"] == "Precleared is limiting how fast Sellbird can check"


def test_the_rate_limit_hold_says_what_happens_next():
    message = hold_message({"kind": "rate_limited", "waiting": 2})
    assert message["title"] == "Precleared is limiting how fast Sellbird can check"
    assert message["detail"].startswith("2 cold emails are waiting.")
    assert "next cycle" in message["detail"]
    assert "free key" not in message["detail"]
    assert message["url"] == ""


@pytest.mark.asyncio
async def test_a_key_cut_off_at_the_end_of_an_error_message_is_still_hidden(state):
    key = "pc_live_secret123"
    client = PreclearedClient("GB", api_key=key, transport=mock_raw(
        500, ("x" * 190 + key).encode()))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.pause_checks
    assert "pc_live" not in result.reason


@pytest.mark.asyncio
async def test_a_deeply_nested_402_answer_falls_back_to_the_buy_page(state):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_raw(
        402, b"[" * 100_000 + b"]" * 100_000))

    result = await client.clear("person@example.com", state)

    assert result.held and result.pause_checks
    assert result.fix_url == BUY_URL
