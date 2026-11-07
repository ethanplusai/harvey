"""Suppressions: addresses and domains never emailed, whoever asks."""

import asyncio
from datetime import datetime, timezone

import pytest
from sellbird.integrations.mail_provider import InboundMessage
from sellbird.state import StateManager
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_handler, make_sender, seed_campaign, seed_prospect, state,
)

def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest.mark.asyncio
async def test_add_and_match_address_and_domain(state):
    assert await state.add_suppression("address", " Pat@Example.com ", "opted_out", "reply")
    assert not await state.add_suppression("address", "pat@example.com", "opted_out")  # once
    assert await state.add_suppression("domain", "www.Blocked.example.org", "manual")
    assert await state.is_suppressed("PAT@example.com") == "opted_out"
    assert await state.is_suppressed("anyone@blocked.example.org") == "manual"
    assert await state.is_suppressed("sam@example.com") is None
    found = await state.suppressed_among(["pat@example.com", "a@blocked.example.org", "x@example.net"])
    assert found == {"pat@example.com": "opted_out", "a@blocked.example.org": "manual"}
    rows = await state.list_suppressions()
    assert {(r["kind"], r["value"]) for r in rows} == {("address", "pat@example.com"),
                                                        ("domain", "blocked.example.org")}


@pytest.mark.asyncio
async def test_domain_suppression_matches_every_address(state):
    await state.add_suppression("domain", "example.org")
    emails = [f"person{i}@example.org" for i in range(700)]  # more than one query chunk
    assert len(await state.suppressed_among(emails)) == 700


@pytest.mark.asyncio
async def test_bad_input_refused(state):
    for kind, value in (("address", "not-an-email"), ("phone", "x"), ("address", "")):
        with pytest.raises(ValueError):
            await state.add_suppression(kind, value)


@pytest.mark.asyncio
async def test_opt_out_reply_suppresses_address(state):
    pid = await seed_prospect(state, status="contacted")
    queued = await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
                                         send_at=_now_iso(), status="approved", campaign_id="c1", step=2)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="in1", from_email="jane@acme.com",
                                       subject="Re: hello", body="Please remove me from your list.")]
    await make_handler(state, provider, intent="unsubscribe")._run_native()
    assert await state.is_suppressed("jane@acme.com") == "opted_out"
    assert (await state.get_outbox_item(queued))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_bounce_suppresses_address(state):
    pid = await seed_prospect(state, status="contacted")
    item = await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
                                       send_at=_now_iso(), status="approved", campaign_id="c1", step=1)
    await state.update_outbox_item(item, status="sent", message_id="<m1@x>", sent_at=_now_iso())
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="b1", from_email="mailer-daemon@googlemail.com",
                                       subject="Delivery Status Notification (Failure)", body="bounced",
                                       in_reply_to="<m1@x>", is_bounce=True)]
    await make_handler(state, provider)._run_native()
    assert await state.is_suppressed("jane@acme.com") == "bounced"


@pytest.mark.asyncio
async def test_sender_never_stages_or_sends_suppressed(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    await state.add_suppression("domain", "acme.com", "manual")
    sender = make_sender(state, FakeProvider(), require_approval=False)
    await sender._run_native()
    assert await state.get_outbox(status="pending_review") == []
    assert await state.get_outbox(status="approved") == []
    # Already queued before the suppression: cancelled at send time, never sent.
    pid2 = await seed_prospect(state, email="sam@other.example.com")
    item = await state.add_outbox_item(prospect_id=pid2, to_email="sam@other.example.com", subject="hello there",
                                       body="A short, plain note. Question?", send_at=_now_iso(),
                                       status="approved", kind="reply")
    await state.add_suppression("address", "sam@other.example.com", "complaint")
    await sender._run_native()
    assert sender.provider.sent == []
    row = await state.get_outbox_item(item)
    assert row["status"] == "cancelled" and "suppressed" in (row.get("error") or "")


@pytest.mark.asyncio
async def test_domain_given_as_a_url_or_address_is_normalised(state):
    assert await state.add_suppression("domain", "https://www.Example.org/contact?x=1")
    assert [r["value"] for r in await state.list_suppressions()] == ["example.org"]
    assert not await state.add_suppression("domain", "pat@example.org")  # the same domain
    with pytest.raises(ValueError):
        await state.add_suppression("domain", "https:///")


@pytest.mark.asyncio
async def test_a_suppressed_domain_covers_its_subdomains(state):
    await state.add_suppression("domain", "example.org")
    assert await state.is_suppressed("pat@mail.sales.example.org") == "manual"
    assert await state.is_suppressed("pat@notexample.org") is None


def test_cli_lists_adds_and_removes(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from sellbird import cli
    db = str(tmp_path / "cli.db")
    monkeypatch.setattr("sellbird.state.DB_PATH", db)
    assert cli.cmd_suppressions(SimpleNamespace(suppression_action="add", value="www.Example.org")) is None
    assert "Suppressed: example.org (domain)" in capsys.readouterr().out
    assert cli.cmd_suppressions(SimpleNamespace(suppression_action="list", value="")) is None
    listed = capsys.readouterr().out
    sid = listed.split()[0]
    assert "example.org" in listed
    assert cli.cmd_suppressions(SimpleNamespace(suppression_action="remove", value=sid)) is None
    assert cli.cmd_suppressions(SimpleNamespace(suppression_action="list", value="")) is None
    assert "Nobody is suppressed" in capsys.readouterr().out


def test_cli_exits_nonzero_on_a_bad_value(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sellbird import cli
    monkeypatch.setattr("sellbird.state.DB_PATH", str(tmp_path / "cli.db"))
    with pytest.raises(SystemExit) as e:
        cli.cmd_suppressions(SimpleNamespace(suppression_action="add", value="not an address"))
    assert e.value.code == 1


@pytest.mark.asyncio
async def test_undoing_an_opt_out_lifts_only_its_suppression(state):
    pid = await seed_prospect(state, status="opted_out")
    await state.add_suppression("address", "jane@acme.com", "opted_out", "reply")
    await state.add_suppression("domain", "blocked.example.org", "manual")
    assert await state.undo_opt_out(pid)
    assert await state.is_suppressed("jane@acme.com") is None
    assert await state.is_suppressed("x@blocked.example.org") == "manual"


@pytest.mark.asyncio
async def test_undoing_an_opt_out_keeps_a_bounce(state):
    pid = await seed_prospect(state, status="opted_out")
    await state.add_suppression("address", "jane@acme.com", "bounced", "bounce")
    assert await state.undo_opt_out(pid)
    assert await state.is_suppressed("jane@acme.com") == "bounced"
