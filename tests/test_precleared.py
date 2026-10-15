"""Tests for the optional Precleared check in the native send path: off by
default, one check per address, blocked verdicts fail the outbox item, an
unavailable check holds cold email without failing it, replies are never
checked. No network: every Precleared call goes to an httpx.MockTransport."""

import json
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from harvey.config import PreclearedConfig
from harvey.integrations.precleared import SETTINGS_PREFIX, PreclearedClient
from harvey.state import StateManager
from tests.test_outbox_native import (
    Cfg,
    Env,
    FakeProvider,
    _now_iso,
    make_sender,
    seed_prospect,
)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


OK_BODY = {
    "verdict": "ok_to_email",
    "confidence": "high",
    "reasons": [{
        "rule_key": "gb_corporate_ok",
        "text": "Corporate subscribers need no prior consent.",
        "source_title": "PECR reg. 22",
    }],
    "next_step": "Send, with a footer that says who you are and how to opt out.",
    "receipt": {"id": "cc_rcpt_OK0000000001"},
}

CONSENT_BODY = {
    "verdict": "consent_needed",
    "confidence": "high",
    "reasons": [{
        "rule_key": "gb_individual_consent",
        "text": "Sole traders count as individuals, so consent is needed.",
        "source_title": "PECR reg. 22(2)",
    }],
    "next_step": "Do not send cold email. Get consent first.",
    "receipt": {"id": "cc_rcpt_NO0000000001"},
}


def mock_precleared(calls, body=None, status=200):
    """A Precleared stand-in that records each request."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"json": json.loads(request.content), "headers": dict(request.headers)})
        if status != 200:
            return httpx.Response(status, json={"error": "daily_limit", "message": "Limit reached."})
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


def with_check(sender, transport, api_key=""):
    sender.precleared = PreclearedClient(sender_country="GB", api_key=api_key, transport=transport)
    return sender


async def queue(state, pid, step=1, kind="sequence", to="jane@acme.com"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="hello",
        body="Fine body. Question?", send_at=_now_iso(), status="approved",
        campaign_id="c1" if kind == "sequence" else "", step=step, kind=kind,
    )


async def logged_receipts(state):
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(
            "SELECT details_json FROM actions WHERE action_type = 'email_sent'"
        ) as cursor:
            rows = await cursor.fetchall()
    return [json.loads(r[0]).get("precleared_receipt", "") for r in rows]


# ── off by default ──


def test_check_is_off_unless_enabled():
    assert PreclearedClient.from_config(Cfg(), Env()) is None
    off = SimpleNamespace(compliance=SimpleNamespace(precleared=PreclearedConfig()))
    assert PreclearedClient.from_config(off, Env()) is None


@pytest.mark.asyncio
async def test_disabled_sender_sends_without_a_check(state):
    pid = await seed_prospect(state)
    await queue(state, pid)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    assert sender.precleared is None
    await sender._drain_due()
    assert len(provider.sent) == 1


# ── config ──


def test_config_needs_a_sender_country_when_enabled():
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True)
    cfg = PreclearedConfig(enabled=True, sender_country="gb")
    assert cfg.sender_country == "GB"
    assert cfg.address_source == "unknown"
    with pytest.raises(ValidationError):
        PreclearedConfig(enabled=True, sender_country="US", address_source="guessed")


def test_from_config_reads_settings_key_and_pitch():
    config = SimpleNamespace(
        compliance=SimpleNamespace(precleared=PreclearedConfig(
            enabled=True, sender_country="US", address_source="published",
        )),
        product=SimpleNamespace(description="Route planning for delivery firms"),
    )
    client = PreclearedClient.from_config(config, SimpleNamespace(precleared_api_key="pc_live_test"))
    assert client.sender_country == "US"
    assert client.address_source == "published"
    assert client.api_key == "pc_live_test"
    assert client.pitch == "Route planning for delivery firms"


# ── verdicts ──


@pytest.mark.asyncio
async def test_ok_verdict_sends_and_logs_the_receipt(state):
    pid = await seed_prospect(state)
    await queue(state, pid)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await sender._drain_due()

    assert len(provider.sent) == 1
    assert calls[0]["json"] == {
        "recipient_email": "jane@acme.com",
        "sender_country": "GB",
        "address_source": "unknown",
        "existing_customer": False,
    }
    assert "authorization" not in calls[0]["headers"]
    assert await logged_receipts(state) == ["cc_rcpt_OK0000000001"]


@pytest.mark.asyncio
async def test_api_key_is_sent_as_bearer(state):
    pid = await seed_prospect(state)
    await queue(state, pid)
    calls = []
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_precleared(calls, OK_BODY), api_key="pc_live_test")
    await sender._drain_due()
    assert calls[0]["headers"]["authorization"] == "Bearer pc_live_test"


@pytest.mark.asyncio
async def test_consent_needed_fails_the_item_with_the_reason(state):
    pid = await seed_prospect(state)
    await queue(state, pid)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await sender._drain_due()

    assert provider.sent == []
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1
    assert failed[0]["error"].startswith("precleared: consent_needed")
    assert "PECR reg. 22(2)" in failed[0]["error"]
    assert "Get consent first" in failed[0]["error"]


@pytest.mark.asyncio
async def test_one_check_per_address(state):
    pid = await seed_prospect(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, OK_BODY))

    await queue(state, pid, step=1)
    await sender._drain_due()
    await queue(state, pid, step=2)
    await sender._drain_due()

    assert len(provider.sent) == 2
    assert len(calls) == 1
    assert await state.get_setting(SETTINGS_PREFIX + "jane@acme.com")


@pytest.mark.asyncio
async def test_blocked_verdict_is_remembered_for_later_steps(state):
    pid = await seed_prospect(state)
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await queue(state, pid, step=1)
    await sender._drain_due()
    await queue(state, pid, step=2)
    await sender._drain_due()

    assert provider.sent == []
    assert len(await state.get_outbox(status="failed")) == 2
    assert len(calls) == 1


# ── check unavailable ──


@pytest.mark.asyncio
async def test_daily_limit_holds_cold_email_but_replies_still_go(state):
    pid = await seed_prospect(state)
    other = await seed_prospect(state, email="sam@beta.com")
    await queue(state, pid)
    await queue(state, other, to="sam@beta.com")
    reply_pid = await seed_prospect(state, email="ann@gamma.com", status="replied")
    await queue(state, reply_pid, kind="reply", to="ann@gamma.com")
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, status=429))

    await sender._drain_due()

    # One failed call pauses the check for the rest of the cycle.
    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == ["ann@gamma.com"]
    assert await state.get_outbox(status="failed") == []
    waiting = await state.get_outbox(status="approved")
    assert sorted(i["to_email"] for i in waiting) == ["jane@acme.com", "sam@beta.com"]
    # Nothing cached, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + "jane@acme.com") == ""


@pytest.mark.asyncio
async def test_network_error_holds_the_email(state):
    pid = await seed_prospect(state)
    await queue(state, pid)

    def boom(request):
        raise httpx.ConnectError("no route to host")

    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(boom))
    await sender._drain_due()

    assert provider.sent == []
    assert await state.get_outbox(status="failed") == []
    assert len(await state.get_outbox(status="approved")) == 1


@pytest.mark.asyncio
async def test_replies_are_never_checked(state):
    pid = await seed_prospect(state, status="replied")
    await queue(state, pid, kind="reply")
    calls = []
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared(calls, CONSENT_BODY))

    await sender._drain_due()

    assert calls == []
    assert len(provider.sent) == 1


# ── review regressions (PR #8) ──


@pytest.mark.asyncio
async def test_changed_decision_inputs_require_a_new_check(state):
    calls = []
    initial = PreclearedClient("US", address_source="published", pitch="Delivery software",
                               transport=mock_precleared(calls, OK_BODY))
    assert (await initial.clear("person@example.com", state)).allowed

    updated = PreclearedClient("GB", address_source="unknown", pitch="Unrelated offer",
                               transport=mock_precleared(calls, CONSENT_BODY))
    result = await updated.clear("person@example.com", state)

    assert not result.allowed, f"Stale approval reused; upstream calls={len(calls)}"
    assert result.verdict == "consent_needed"
    assert result.receipt_id == "cc_rcpt_NO0000000001"
    assert len(calls) == 2
    # The new verdict replaced the old one, so asking again with the new facts is cached.
    assert not (await updated.clear("person@example.com", state)).allowed
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_legacy_address_only_cache_entry_is_not_reused(state):
    await state.set_setting(SETTINGS_PREFIX + "person@example.com", json.dumps({
        "verdict": "ok_to_email", "reason": "", "receipt_id": "cc_rcpt_OLD0000000001",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }))
    calls = []
    client = PreclearedClient("GB", transport=mock_precleared(calls, CONSENT_BODY))

    result = await client.clear("person@example.com", state)

    assert not result.allowed
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_held_cold_backlog_does_not_starve_replies(state):
    pid = await seed_prospect(state)
    for step in range(24):
        await queue(state, pid, step=step + 1)
    reply_pid = await seed_prospect(state, email="reply@example.com", status="replied")
    await queue(state, reply_pid, kind="reply", to="reply@example.com")
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], status=429))

    await sender._drain_due()
    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == ["reply@example.com"]
    assert len(await state.get_outbox(status="approved")) == 24
    assert await state.get_outbox(status="failed") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], "ok_to_email", {"verdict": "maybe"}, {"reasons": []}])
async def test_malformed_response_holds_cold_email_and_reply_still_sends(state, body):
    pid = await seed_prospect(state)
    await queue(state, pid)
    reply_pid = await seed_prospect(state, email="reply@example.com", status="replied")
    await queue(state, reply_pid, kind="reply", to="reply@example.com")
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), mock_precleared([], body=body))

    await sender._drain_due()

    assert [m["to"] for m in provider.sent] == ["reply@example.com"]
    assert await state.get_outbox(status="failed") == []
    assert [i["to_email"] for i in await state.get_outbox(status="approved")] == ["jane@acme.com"]
    # Nothing cached from a malformed answer, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + "jane@acme.com") == ""


@pytest.mark.asyncio
async def test_unexpected_optional_field_shapes_are_ignored(state):
    body = {
        "verdict": "consent_needed",
        "reasons": {"text": "not a list"},
        "next_step": 5,
        "receipt": "cc_rcpt_not_an_object",
    }
    client = PreclearedClient("GB", transport=mock_precleared([], body=body))

    result = await client.clear("person@example.com", state)

    assert not result.allowed and not result.held
    assert result.verdict == "consent_needed"
    assert result.reason == ""
    assert result.receipt_id == ""


# ── allowance messages ──


def mock_status(status, body):
    """A Precleared stand-in that answers every request with one status and body."""
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


@pytest.mark.asyncio
async def test_keyless_daily_limit_points_to_a_free_key(state):
    client = PreclearedClient("GB", transport=mock_status(429, {
        "error": "daily_limit", "get_key_url": "https://precleared.dev/get-key",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert "add PRECLEARED_API_KEY" in result.reason
    assert "precleared.dev/get-key" in result.reason


@pytest.mark.asyncio
async def test_key_out_of_checks_points_to_a_pack_not_a_free_key(state):
    client = PreclearedClient("GB", api_key="pc_live_test", transport=mock_status(402, {
        "error": "payment_required", "buy_url": "https://precleared.dev/#pricing",
    }))

    result = await client.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert "pack" in result.reason
    assert "precleared.dev/#pricing" in result.reason
    assert "add PRECLEARED_API_KEY" not in result.reason
    # Nothing cached, so the next cycle asks again.
    assert await state.get_setting(SETTINGS_PREFIX + "person@example.com") == ""
