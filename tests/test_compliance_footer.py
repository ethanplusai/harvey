"""Cold email carries a postal address and an opt-out line (CAN-SPAM), and
replies are read without the quoted email they answer.

Based on community PR #13 by Camilo Cruz.
"""

from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harvey.agents.sender import Sender
from harvey.config import ComplianceConfig, EnvConfig, compliance_notice
from harvey.gate import pre_send_check
from harvey.integrations.mail_provider import InboundMessage, strip_quoted_reply
from harvey.models.campaign import Campaign, EmailStep
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, StubBrain, make_handler, make_sender, seed_prospect, state,
)

ADDRESS = "Acme Co, 100 Main St, Austin, TX 78701"
LINE = ComplianceConfig().opt_out_line


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def _approved(state, pid, kind="sequence", body="A short, plain note. Question?"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello there",
        body=body, send_at=_now_iso(), status="approved",
        campaign_id="c1" if kind == "sequence" else "", step=2, kind=kind,
    )


def _sender(state, provider, address=ADDRESS, line=LINE):
    sender = make_sender(state, provider)
    sender.config.compliance = ComplianceConfig(postal_address=address, opt_out_line=line)
    return sender


# ── The footer ──

@pytest.mark.asyncio
async def test_cold_email_goes_out_with_the_address_and_opt_out_line(state):
    pid = await seed_prospect(state, status="contacted")
    item_id = await _approved(state, pid)
    provider = FakeProvider()
    await _sender(state, provider)._drain_due()
    assert provider.sent[0]["body"] == (
        "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE)
    # The Outbox keeps what actually went out.
    assert (await state.get_outbox_item(item_id))["body"] == provider.sent[0]["body"]


@pytest.mark.asyncio
async def test_replies_get_no_footer(state):
    pid = await seed_prospect(state, status="replied")
    await _approved(state, pid, kind="reply", body="Sure, Tuesday works. Talk then?")
    provider = FakeProvider()
    await _sender(state, provider)._drain_due()
    assert provider.sent[0]["body"] == "Sure, Tuesday works. Talk then?"


@pytest.mark.asyncio
async def test_without_an_address_cold_email_still_goes_with_the_opt_out_line(state, caplog):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)
    provider = FakeProvider()
    await _sender(state, provider, address="")._drain_due()
    assert provider.sent[0]["body"] == "A short, plain note. Question?\n\n" + LINE
    assert "postal_address" in caplog.text


@pytest.mark.asyncio
async def test_both_lines_can_be_turned_off(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)
    provider = FakeProvider()
    await _sender(state, provider, address="", line="")._drain_due()
    assert provider.sent[0]["body"] == "A short, plain note. Question?"


@pytest.mark.asyncio
async def test_a_footer_already_in_the_body_is_not_added_twice(state):
    pid = await seed_prospect(state, status="contacted")
    body = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE
    await _approved(state, pid, body=body)
    provider = FakeProvider()
    await _sender(state, provider)._drain_due()
    assert provider.sent[0]["body"] == body


@pytest.mark.asyncio
async def test_old_configs_without_a_compliance_section_still_send(state):
    # Test configs (and any hand-built config) may lack the section entirely.
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent[0]["body"].endswith(LINE)


def test_the_gate_refuses_cold_email_missing_its_configured_address():
    ok = pre_send_check("jane@acme.com", "hello there", "A note. Question?",
                        postal_address=ADDRESS)
    assert not ok and any("postal address" in r for r in ok.reasons)
    assert pre_send_check("jane@acme.com", "hello there",
                          "A note. Question?\n\n" + ADDRESS, postal_address=ADDRESS)
    # Replies never need it.
    assert pre_send_check("jane@acme.com", "hello there", "Sure, Tuesday works.",
                          kind="reply", postal_address=ADDRESS)


@pytest.mark.asyncio
async def test_instantly_sequences_carry_the_footer(state):
    config = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            enabled=True, provider="instantly", require_approval=False,
            max_daily_sends=50, send_to_risky=False)),
        compliance=ComplianceConfig(postal_address=ADDRESS),
    )
    sender = Sender(None, state, config, EnvConfig())
    sender.instantly = AsyncMock()
    sender.instantly.set_campaign_emails.return_value = None  # stop after the upload
    campaign = Campaign(id="c", name="Draft", instantly_campaign_id="existing",
                        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")])
    await sender._deploy_campaign(campaign)
    uploaded = sender.instantly.set_campaign_emails.await_args.args[1]
    assert uploaded[0]["body"] == "Would this help?\n\n" + ADDRESS + "\n" + LINE


def test_the_notice_asks_for_an_address_only_when_email_is_on():
    def cfg(address, enabled=True):
        return SimpleNamespace(
            channels=SimpleNamespace(email=SimpleNamespace(enabled=enabled)),
            compliance=ComplianceConfig(postal_address=address))
    assert "compliance.postal_address" in compliance_notice(cfg(""))
    assert compliance_notice(cfg(ADDRESS)) == ""
    assert compliance_notice(cfg("", enabled=False)) == ""
    assert compliance_notice(cfg("   ")) != ""


# ── Reading replies ──

GMAIL_QUOTE = (
    "Sounds interesting, send me pricing.\n\n"
    "On Tue, Oct 14, 2026 at 3:02 PM Harvey <harvey@x.co> wrote:\n"
    "> A short, plain note. Question?\n>\n> " + ADDRESS + "\n> " + LINE
)
OUTLOOK_QUOTE = (
    "Sounds interesting, send me pricing.\n\n"
    "From: Harvey <harvey@x.co>\nSent: Tuesday, October 14, 2026 3:02 PM\n"
    "To: Jane\nSubject: hello there\n\n"
    "A short, plain note. Question?\n\n" + ADDRESS + "\n"
    # Outlook re-wraps lines and uses curly quotes.
    "Not interested? Reply “no\nthanks” and I won’t email again."
)


def test_strip_keeps_only_what_they_wrote():
    assert strip_quoted_reply(GMAIL_QUOTE) == "Sounds interesting, send me pricing."
    assert strip_quoted_reply(OUTLOOK_QUOTE) == "Sounds interesting, send me pricing."
    assert strip_quoted_reply(
        "Thanks.\n\n-----Original Message-----\nFrom: x") == "Thanks."
    # Answers written between quoted lines are theirs too.
    assert strip_quoted_reply(
        "Hi Harvey,\n\n> Would you be open to a call?\n\nNo. Remove me please."
    ) == "Hi Harvey,\n\nNo. Remove me please."
    # Everything under a quote header: keep their lines, never return nothing.
    assert "Remove me" in strip_quoted_reply(
        "On Tue Harvey wrote:\n> Open to a call?\nRemove me")
    assert strip_quoted_reply("") == ""


class RecordingBrain(StubBrain):
    def __init__(self, intent):
        super().__init__(intent=intent)
        self.prompts = []

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.prompts.append(prompt)
        return await super().think(prompt, session_id=session_id, agent=agent, task=task)


async def _reply(state, body, intent="interested"):
    pid = await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com", subject="Re: hello there", body=body)]
    handler = make_handler(state, provider, intent=intent)
    handler.config.compliance = ComplianceConfig(postal_address=ADDRESS)
    handler.brain = RecordingBrain(intent)
    await handler._run_native()
    return (await state.get_prospect(pid)).status, handler.brain.prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    GMAIL_QUOTE, OUTLOOK_QUOTE,
    # An HTML-only reply converted to text keeps its entities.
    "Sounds interesting.\n\nOn Tue Harvey wrote:\nNot interested? Reply &quot;no thanks&quot; "
    "and I won&#39;t email again.",
    # Gmail re-wraps long quoted lines.
    "Sounds interesting.\n\nOn Tue Harvey wrote:\n> Not interested? Reply \"no\n> thanks\" "
    "and I won't email again.",
], ids=["gmail", "outlook", "html-entities", "gmail-wrapped"])
async def test_our_own_quoted_footer_never_reads_as_an_opt_out(state, body):
    status, prompts = await _reply(state, body)
    assert status != "opted_out"
    # The model reads their words, not our quoted email.
    assert prompts and all(ADDRESS not in p for p in prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "No thanks.",
    "no thank you\n\nOn Tue Harvey wrote:\n> A short, plain note.",
    # Answered inline, below and between our quoted lines.
    "> Would you be open to a quick call?\nNo. Please remove me from your list.",
    "Hi Harvey,\n\n> Would you be open to a quick call?\n\nNo. Remove me from your list.",
    "On Tue Harvey wrote:\n> Open to a call?\nunsubscribe",
], ids=["no-thanks", "above-quote", "below-quote", "between-quotes", "under-header"])
async def test_an_opt_out_is_honoured_wherever_they_wrote_it(state, body):
    status, _ = await _reply(state, body)
    assert status == "opted_out"


@pytest.mark.asyncio
async def test_a_legal_threat_under_the_quote_still_escalates(state):
    status, prompts = await _reply(
        state, "> A short, plain note.\nMy attorney will be in touch.")
    assert status != "opted_out"
    assert prompts == []  # escalated by keyword, no model call, no auto-reply


# ── Dashboard ──

from tests.test_dashboard_manual_send import client, env  # noqa: E402,F401


def _config(address):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(enabled=True)),
        compliance=ComplianceConfig(postal_address=address))


def test_the_outbox_shows_the_footer_and_asks_for_a_missing_address(client, monkeypatch):
    import harvey.config
    monkeypatch.setattr(harvey.config, "load_config", lambda: _config(""))
    data = client.get("/api/outbox").json()
    assert data["footer"] == LINE
    assert "postal_address" in data["compliance_notice"]

    monkeypatch.setattr(harvey.config, "load_config", lambda: _config(ADDRESS))
    data = client.get("/api/outbox").json()
    assert data["footer"] == ADDRESS + "\n" + LINE
    assert data["compliance_notice"] == ""


def test_an_unreadable_config_still_shows_the_outbox(client, monkeypatch):
    import harvey.config

    def broken():
        raise harvey.config.ConfigError("bad yaml")
    monkeypatch.setattr(harvey.config, "load_config", broken)
    data = client.get("/api/outbox").json()
    assert data["footer"] == "" and "pending" in data


# ── Review round 1: false opt-outs, missed opt-outs ──

async def _reply_after_sending(state, body, sent_bodies=(), intent="interested",
                               line=LINE):
    """A reply from someone Harvey already emailed `sent_bodies`."""
    pid = await seed_prospect(state, status="contacted")
    for n, sent in enumerate(sent_bodies, start=1):
        oid = await state.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject="hello there",
            body=sent, send_at=_now_iso(), status="approved", campaign_id="c1", step=n)
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso())
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com", subject="Re: hello there", body=body)]
    handler = make_handler(state, provider, intent=intent)
    handler.config.compliance = ComplianceConfig(postal_address=ADDRESS, opt_out_line=line)
    handler.brain = RecordingBrain(intent)
    await handler._run_native()
    return (await state.get_prospect(pid)).status, handler.brain.prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Do you offer a monthly plan, yes or no\nThanks,\nBob",
    "Sounds good, call me.\n\nMario Bruno\nThanks for reaching out",
    "Interested! Can we talk next month? No Thanksgiving week though.",
], ids=["yes-or-no", "signature", "thanksgiving"])
async def test_no_thanks_only_counts_as_the_phrase_itself(state, body):
    status, _ = await _reply(state, body)
    assert status != "opted_out"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["No, thanks.", "STOP", "Stop.", "  unsubscribe  "],
                         ids=["comma", "stop", "stop-dot", "bare-unsubscribe"])
async def test_short_opt_out_replies_are_caught_without_the_model(state, body):
    status, prompts = await _reply(state, body)
    assert status == "opted_out" and prompts == []


@pytest.mark.asyncio
async def test_quoting_an_old_opt_out_line_is_not_an_opt_out(state):
    # Emails went out with a custom line; the line was changed afterwards.
    old = 'Reply "unsubscribe" to stop hearing from me.'
    sent = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + old
    status, _ = await _reply_after_sending(
        state, "Yes, let's talk Tuesday.\n\nOn Tue Harvey wrote:\n> " + sent.replace("\n", "\n> "),
        sent_bodies=[sent], line="Not for you? Reply no thanks.")
    assert status != "opted_out"


# Dutch Outlook: a header strip_quoted_reply doesn't know.
DUTCH_OUTLOOK = (
    "Klinkt goed, stuur me de prijzen.\n\n"
    "Van: Harvey <harvey@x.co>\nVerzonden: dinsdag 14 oktober 2026 15:02\n\n"
    "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE
)


@pytest.mark.asyncio
async def test_our_quoted_email_never_reaches_the_model_even_unrecognised(state):
    sent = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE
    status, prompts = await _reply_after_sending(state, DUTCH_OUTLOOK, sent_bodies=[sent])
    assert status != "opted_out"
    assert prompts and all("no thanks" not in p.lower() for p in prompts)
    assert any("Klinkt goed" in p for p in prompts)


@pytest.mark.asyncio
async def test_a_footer_cut_off_by_the_length_limit_is_not_an_opt_out(state):
    sent = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE
    body = ("Interested, tell me more.\n\nOn Tue Harvey wrote:\n> A short, plain note. Question?\n> "
            + ADDRESS + '\n> Not interested? Reply "no thanks"')  # truncated here
    status, _ = await _reply_after_sending(state, body, sent_bodies=[sent])
    assert status != "opted_out"


@pytest.mark.asyncio
async def test_escalation_words_in_our_own_quoted_email_dont_escalate(state):
    sent = "We help attorney offices fill their calendar. Worth a chat?"
    status, prompts = await _reply_after_sending(
        state, "Yes please.\n\nOn Tue Harvey wrote:\n> " + sent, sent_bodies=[sent])
    assert prompts  # classified by the model, not escalated by keyword


def test_inline_answers_under_a_quote_header_are_kept():
    text = strip_quoted_reply(
        "Answers inline below.\n\nOn Tue, Oct 14 Harvey wrote:\n"
        "> Would a call work?\nYes, Thursday 2pm.\n> CRM?\nHubSpot.")
    assert "Thursday 2pm" in text and "HubSpot" in text and "Would a call" not in text


def test_an_underscore_line_of_their_own_is_not_a_quote():
    assert "pricing" in strip_quoted_reply("Hi Ethan,\n____________\nSend pricing please.")


# ── Review round 1: the footer itself ──

@pytest.mark.asyncio
async def test_a_short_address_mentioned_in_the_body_still_gets_the_footer(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid, body="Saw you're in Austin, TX. Question?")
    provider = FakeProvider()
    await _sender(state, provider, address="Austin, TX")._drain_due()
    assert provider.sent[0]["body"].endswith("\n\nAustin, TX\n" + LINE)


def test_opt_out_line_must_be_recognisable_and_link_free():
    import pydantic
    for bad in ("To unsubscribe, visit https://acme.com/u", "Click here to leave",
                "Thanks for reading!"):
        with pytest.raises(pydantic.ValidationError):
            ComplianceConfig(opt_out_line=bad)
    assert ComplianceConfig(opt_out_line="Reply STOP to opt out.").opt_out_line
    assert ComplianceConfig(opt_out_line="").opt_out_line == ""
    # Left blank in YAML (null) means empty, not a crash.
    assert ComplianceConfig(postal_address=None, opt_out_line=None).opt_out_line == ""


def test_no_opt_out_line_is_warned_about_too():
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(enabled=True)),
        compliance=ComplianceConfig(postal_address=ADDRESS, opt_out_line=""))
    assert "opt_out_line" in compliance_notice(cfg)


def test_an_edit_the_footer_would_push_over_the_limit_is_refused(client, monkeypatch, env):
    import sqlite3
    import harvey.config
    monkeypatch.setattr(harvey.config, "load_config", lambda: SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(enabled=True)),
        compliance=ComplianceConfig(postal_address=ADDRESS)))
    with closing(sqlite3.connect(env["db"])) as db, db:
        db.execute("INSERT INTO outbox (id, prospect_id, campaign_id, step, kind, to_email, "
                   "subject, body, send_at, status) VALUES ('o1', 'p1', 'c1', 1, 'sequence', "
                   "'a@acme.test', 's', 'b', '2026-01-01T00:00:00', 'pending_review')")
    body = " ".join(["word"] * 215)  # fits alone, not with the footer
    r = client.post("/api/outbox/o1/edit", json={"subject": "s", "body": body})
    assert r.status_code == 400 and "too long" in r.json()["message"]


def test_campaigns_carry_the_footer_for_sending_by_hand(client, monkeypatch, env):
    import harvey.config
    monkeypatch.setattr(harvey.config, "load_config", lambda: _config(ADDRESS))
    from tests.test_dashboard_manual_send import _campaign, _prospect
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    assert client.get("/api/campaigns").json()[0]["footer"] == ADDRESS + "\n" + LINE


# ── Review round 2 ──

SENT = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE


@pytest.mark.asyncio
async def test_an_html_only_reply_quoting_our_footer_is_not_an_opt_out(state):
    # Tags become spaces: their words and our quote end up on one line.
    body = ("Yes, interested. Tuesday works.   On Tue, Oct 14, 2026 Harvey wrote:   "
            "A short, plain note. Question?   " + ADDRESS + "   "
            "Not interested? Reply &quot;no thanks&quot; and I won&#39;t email again.")
    status, prompts = await _reply_after_sending(state, body, sent_bodies=[SENT])
    assert status != "opted_out"
    assert prompts and all("no thanks" not in p.lower() for p in prompts)


@pytest.mark.asyncio
async def test_short_wrapped_fragments_of_our_quote_are_ours(state):
    body = ("Sure, call me.\n\nOn Tue Harvey wrote:\n> A short, plain note. Question?\n"
            "> Not interested? Reply\n> \"no thanks\"\n> and I won't email again.")
    status, _ = await _reply_after_sending(state, body, sent_bodies=[SENT])
    assert status != "opted_out"


@pytest.mark.asyncio
async def test_outlook_mailto_annotations_dont_hide_our_line(state):
    line = "To opt out, reply STOP to this email."
    sent = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + line
    body = ("Yes please, send pricing.\n\nFrom: Harvey <harvey@x.co<mailto:harvey@x.co>>\n"
            "Sent: Tuesday\n\nA short, plain note. Question?\n" + ADDRESS + "\n"
            "To opt out, reply STOP to this email<https://x.co/a>.")
    status, _ = await _reply_after_sending(state, body, sent_bodies=[sent], line=line)
    assert status != "opted_out"


@pytest.mark.asyncio
async def test_campaign_text_counts_as_ours_without_an_outbox_row(state):
    # Instantly sends and hand sends never pass through the Outbox.
    import json
    import sqlite3
    old = 'Reply "unsubscribe" to stop hearing from me.'
    pid = await seed_prospect(state, status="contacted")
    with closing(sqlite3.connect(state.db_path)) as db, db:
        db.execute(
            "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, status) "
            "VALUES ('c9', 'x', 'email', ?, ?, 'active')",
            (json.dumps([{"step": 1, "subject": "hi", "delay_days": 0,
                          "body": "Hi {{first_name}}, we help attorney offices grow.\n\n" + old}]),
             json.dumps([pid])))
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com", subject="Re: hi",
        body="Yes let's talk.\n\nOn Tue Harvey wrote:\n> Hi Jane, we help attorney offices grow.\n> " + old)]
    handler = make_handler(state, provider, intent="interested")
    handler.config.compliance = ComplianceConfig(opt_out_line="Reply no thanks to stop.")
    handler.brain = RecordingBrain("interested")
    await handler._run_native()
    assert (await state.get_prospect(pid)).status != "opted_out"
    assert handler.brain.prompts  # not escalated over our own "attorney"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Is it month to month, yes or no, thanks",
    # "no" ending a line, then a "Thanks" sign-off: never joined into a phrase.
    "Do you require a long contract? Hoping the answer is no\nThanks!\nBob",
    "Monday works, Tuesday no\nThanks, Jim",
    "Is it month to month? yes/no\nThanks,\nBob",
    "Happy to chat Tuesday, no thanks needed for the note!",
], ids=["yes-or-no-comma", "answer-is-no", "tuesday-no", "yes-slash-no", "no-thanks-needed"])
async def test_a_question_ending_in_or_no_thanks_is_not_an_opt_out(state, body):
    status, _ = await _reply(state, body)
    assert status != "opted_out"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "STOP\n\nSent from my iPhone",
    "Stop\n\nGet Outlook for iOS",
    "I'd ask that you do not\ncontact me again.",
], ids=["stop-iphone", "stop-outlook-ios", "wrapped-do-not-contact"])
async def test_opt_outs_with_signatures_or_wrapped_lines_are_caught(state, body):
    status, prompts = await _reply(state, body)
    assert status == "opted_out" and prompts == []


def test_opt_out_line_must_ask_for_a_reply_harvey_catches():
    import pydantic
    for bad in ('Reply "pass" and I\'ll stop.', "To opt out email optout@acme.com"):
        with pytest.raises(pydantic.ValidationError):
            ComplianceConfig(opt_out_line=bad)
    for good in (LINE, "Reply STOP to opt out.", "Not a fit? Just reply with unsubscribe.",
                 "Reply 'remove me' and you won't hear from me again."):
        assert ComplianceConfig(opt_out_line=good).opt_out_line == good


# ── Review round 3 ──

@pytest.mark.asyncio
async def test_every_footer_harvey_used_stays_ours_after_the_line_changes(state):
    # Instantly keeps the footer in its own copy; Harvey remembers what it used.
    old = 'Reply "unsubscribe" to stop hearing from me.'
    config = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            enabled=True, provider="instantly", require_approval=False,
            max_daily_sends=50, send_to_risky=False)),
        compliance=ComplianceConfig(postal_address=ADDRESS, opt_out_line=old))
    sender = Sender(None, state, config, EnvConfig())
    sender.instantly = AsyncMock()
    sender.instantly.set_campaign_emails.return_value = None
    await sender._deploy_campaign(Campaign(
        id="c", name="Draft", instantly_campaign_id="existing",
        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")]))
    # The line is changed afterwards; a reply quotes the old one.
    status, _ = await _reply(state, "Yes let's talk.\n\nOn Tue Harvey wrote:\n> Would this help?\n> " + old)
    assert status != "opted_out"


@pytest.mark.asyncio
async def test_template_lines_with_merge_tags_are_recognised_when_quoted(state):
    import json
    import sqlite3
    pid = await seed_prospect(state, status="contacted")
    with closing(sqlite3.connect(state.db_path)) as db, db:
        db.execute(
            "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, status) "
            "VALUES ('c9', 'x', 'email', ?, ?, 'active')",
            (json.dumps([{"step": 1, "subject": "hi", "delay_days": 0,
                          "body": "We help {{company}} attorney offices grow."}]),
             json.dumps([pid])))
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com", subject="Re: hi",
        body="Yes please.\n\nOn Tue Harvey wrote:\n> We help Acme attorney offices grow.")]
    handler = make_handler(state, provider, intent="interested")
    handler.brain = RecordingBrain("interested")
    await handler._run_native()
    assert handler.brain.prompts  # our pitch's "attorney" didn't escalate


# ── Hebrew ──

HEBREW_LINE = 'לא רלוונטי? השיבו "הסר" ולא אפנה שוב.'


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "הסר",
    "הסירו",
    "'הסר'",
    "הסר בבקשה",
    "הסר, תודה",
    "הסר\n\nנשלח מה-iPhone שלי",
    "\u200fהסר\u200f",                       # direction marks around it
    "הָסֵר",                                  # with vowel points
    "תורידו אותי מהרשימה בבקשה.",
    "אפשר להסיר אותי מהרשימה?",
    "ותורידו אותנו מרשימת התפוצה",
    "תמחקו את המייל שלי",
    "תפסיקו לשלוח לי מיילים.",
    "נא לא לפנות אליי שוב.",
    "אל תכתבו לי יותר.",
    "אל תשלחו לי עוד",
    "אני לא רוצה לקבל יותר מיילים",
    "לא תודה",
    "לא מעוניין, תודה.",
    "לא מעוניינת כרגע, תודה רבה",
    "תודה, אבל אנחנו לא מעוניינים.",
], ids=["bare", "bare-plural", "quoted", "please", "thanks", "iphone-signature", "rlm",
        "niqqud", "take-me-off", "remove-me", "and-take-us-off", "delete-my-email",
        "stop-sending", "dont-contact-again", "dont-write", "dont-send-more",
        "dont-want-emails", "no-thanks", "not-interested-thanks", "not-interested-for-now",
        "thanks-not-interested"])
async def test_hebrew_opt_outs_are_caught_without_the_model(state, body):
    status, prompts = await _reply(state, body)
    assert status == "opted_out" and prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "לא",
    "הסרתי את הקובץ המצורף, אפשר לשלוח שוב?",
    "מעוניין, תודה! מתי נוח לדבר?",
    "זה חודשי, כן או לא, תודה",
    "מקווה שלא\nתודה",
    "לא מעוניין לשלם מראש, תודה. אפשר חודשי?",
    "תודה, לא מעוניין בחבילה הגדולה אבל הקטנה נשמעת טוב",
    "הסר את סעיף 4 ואני חותם",
    "אני מבקש הסרה של סעיף הקנס",
    "הספק הקודם הפסיק לשלוח לנו דוחות, אתם עושים את זה?",
    "אל תשלחו את החוזה עד שנדבר",
    "אנחנו עושים הסרת שיער בלייזר, זה מתאים?",
    "לא רציתי להטריד, אבל יש לי שאלה על המחיר",
], ids=["no-alone", "i-removed", "interested-thanks", "yes-or-no-thanks", "hope-not-then-thanks",
        "not-paying-upfront", "not-the-big-package", "remove-a-clause", "removal-of-a-clause",
        "supplier-stopped", "dont-send-the-contract-yet", "hair-removal", "didnt-want-to-bother"])
async def test_ordinary_hebrew_is_not_an_opt_out(state, body):
    status, prompts = await _reply(state, body)
    assert status != "opted_out" and prompts


@pytest.mark.asyncio
async def test_quoting_our_hebrew_footer_is_not_an_opt_out(state):
    sent = "הודעה קצרה ופשוטה. שאלה?\n\n" + ADDRESS + "\n" + HEBREW_LINE
    status, prompts = await _reply_after_sending(
        state, "כן, בואו נדבר ביום שלישי.\n\nב-יום ג׳ Harvey כתב:\n> " + sent.replace("\n", "\n> "),
        sent_bodies=[sent], line=HEBREW_LINE)
    assert status != "opted_out" and prompts


def test_every_reply_an_opt_out_line_can_ask_for_is_caught():
    from harvey.agents.handler import _opt_out_reason
    from harvey.config import HEBREW_OPT_OUT_WORDS, OPT_OUT_WORDS
    for word in OPT_OUT_WORDS + HEBREW_OPT_OUT_WORDS:
        assert _opt_out_reason(word, word), word


def test_opt_out_line_can_ask_in_hebrew():
    import pydantic
    for good in (HEBREW_LINE, "להסרה השיבו הסר", "להסרה השב 'הסר'",
                 'ואם זה לא בשבילכם, תגידו לי "לא תודה" ולא אכתוב שוב.',
                 "השיבו במייל חוזר ״הסר״ ונסיר אתכם.", "ואם לא מתאים, פשוט ענו לא, תודה.",
                 'בלי לחץ: השיבו "הסר" ולא אפנה שוב.'):
        assert ComplianceConfig(opt_out_line=good).opt_out_line == good
    for bad in (
        "ואם זה לא בשבילכם, תגידו לי ולא אכתוב שוב.",   # names no reply Harvey catches
        'השיבו "הסרתי"',
        "תודה שקראתם!",
        "להסרה לחצו כאן",
        'להסרה השיבו "הסר" או לחצו כאן',
        "להסרה: https://example.com/u",
        "להסרה כתבו ל-remove@example.com",
    ):
        with pytest.raises(pydantic.ValidationError):
            ComplianceConfig(opt_out_line=bad)
