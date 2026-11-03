"""Tests for the native sending path: gate, outbox lifecycle, stage/drain,
stop-on-reply, bounces, and the kill switch."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from harvey.gate import pre_send_check
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class FakeProvider(MailProvider):
    name = "fake"

    def __init__(self):
        self.sent = []
        self.inbound = []
        self.fail_next = False

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to=""):
        if self.fail_next:
            self.fail_next = False
            return SendResult(ok=False, error="smtp boom")
        self.sent.append({"to": to_email, "subject": subject, "body": body,
                          "thread_ref": thread_ref, "in_reply_to": in_reply_to})
        return SendResult(ok=True, message_id=f"<m{len(self.sent)}@x>", thread_ref="t1")

    async def get_replies(self, limit=50):
        return self.inbound

    async def test_connection(self):
        return True, "fake"


class Cfg:
    class persona:
        name = "Harvey"; email = "harvey@x.co"; company = "X"; role = "BD"; tone = "direct"

    class product:
        name = "P"; description = "d"; pricing = "$"; key_benefits = ["b"]
        objection_responses = {}

    class channels:
        class email:
            enabled = True
            provider = "smtp"
            max_daily_sends = 50
            send_to_risky = False
            require_approval = True
            max_bounce_rate = 0.05

        class linkedin:
            enabled = False


class Env:
    instantly_api_key = ""


def make_sender(state, provider, require_approval=True):
    from harvey.agents.sender import Sender
    cfg = Cfg()
    cfg.channels.email.require_approval = require_approval
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.provider = provider  # override registry lookup
    sender.send_pacing = False  # no inter-send sleeps in tests
    return sender


class StubBrain:
    """Minimal brain: canned intent + reply text, no subprocess."""
    def __init__(self, intent="question", reply="Sure, happy to help."):
        self._intent = intent
        self._reply = reply

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        return self._intent if task == "classify_intent" or "category" in prompt.lower() else self._reply


def make_handler(state, provider, intent="question"):
    from harvey.agents.handler import Handler
    handler = Handler(brain=StubBrain(intent=intent), state=state, config=Cfg(), env=Env())
    handler.provider = provider
    return handler


async def seed_prospect(state, email="jane@acme.com", status="new", estatus="verified"):
    return await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="VP", company="Acme",
        email=email, email_status=estatus, email_verified=(estatus == "verified"),
        status=status,
    ))


async def seed_campaign(state, prospect_ids):
    campaign = Campaign(
        id="", name="test-campaign", channel="email",
        sequence=[
            EmailStep(step=1, subject="hi {{first_name}}", body="Body for {{company}}. Question?", delay_days=0),
            EmailStep(step=2, subject="follow", body="Another angle {{first_name}}.", delay_days=3),
        ],
        prospect_ids=prospect_ids, status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


# ── gate ──


def test_gate_blocks_bad_content():
    p = Prospect(first_name="J", last_name="D", title="VP",
                 email="jane@acme.com", email_status="verified")
    ok = pre_send_check("jane@acme.com", "hello", "Short and fine. Question?", p)
    assert ok.ok

    bad = pre_send_check("jane@acme.com", "hello", "Hi {{first_name}}, act now!", p)
    assert not bad.ok
    assert any("merge" in r for r in bad.reasons)
    assert any("banned" in r for r in bad.reasons)

    guess = Prospect(first_name="J", last_name="D", title="VP",
                     email="jane@acme.com", email_status="guess")
    blocked = pre_send_check("jane@acme.com", "hello", "Fine body.", guess)
    assert not blocked.ok

    # Replies to someone who emailed us are exempt from the status gate
    reply_ok = pre_send_check("jane@acme.com", "Re: hi", "Thanks!", guess, kind="reply")
    assert reply_ok.ok


def test_gate_link_and_html_limits():
    r = pre_send_check("a@b.co", "s", "See https://a.com and https://b.com")
    assert any("links" in x for x in r.reasons)
    r2 = pre_send_check("a@b.co", "s", "<div>hello</div>")
    assert any("HTML" in x for x in r2.reasons)


def test_bounce_detection():
    assert looks_like_bounce("mailer-daemon@googlemail.com", "anything")
    assert looks_like_bounce("mx@x.com", "Delivery Status Notification (Failure)")
    assert not looks_like_bounce("jane@acme.com", "Re: your note")


# ── staging ──


@pytest.mark.asyncio
async def test_stage_campaign_renders_and_respects_approval(state):
    pid = await seed_prospect(state)
    campaign = await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider(), require_approval=True)

    await sender._run_native()

    pending = await state.get_outbox(status="pending_review")
    assert len(pending) == 2  # both steps staged
    step1 = next(i for i in pending if i["step"] == 1)
    assert step1["subject"] == "hi Jane"
    assert "Acme" in step1["body"]
    # Nothing sent while awaiting approval
    assert sender.provider.sent == []

    # Idempotent: re-running stages nothing new
    campaign2 = await state.get_campaigns_by_status("active")
    assert campaign2[0].id == campaign.id
    await sender._run_native()
    assert len(await state.get_outbox(status="pending_review")) == 2


@pytest.mark.asyncio
async def test_stage_skips_unverified(state):
    pid = await seed_prospect(state, email="guess@acme.com", estatus="guess")
    await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider())
    await sender._run_native()
    assert await state.get_outbox(status="pending_review") == []


# ── drain ──


@pytest.mark.asyncio
async def test_drain_sends_approved_due_items(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)

    await sender._run_native()  # stage as approved + drain in one pass

    sent = await state.get_outbox(status="sent")
    assert len(sent) == 1                     # step 1 due now; step 2 in 3 days
    assert provider.sent[0]["to"] == "jane@acme.com"
    assert provider.sent[0]["subject"] == "hi Jane"
    prospect = await state.get_prospect(pid)
    assert prospect.status == "contacted"
    approved_left = await state.get_outbox(status="approved")
    assert len(approved_left) == 1 and approved_left[0]["step"] == 2


@pytest.mark.asyncio
async def test_drain_respects_kill_switch(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await state.set_setting("sending_paused", "test pause")

    await sender._run_native()
    assert provider.sent == []
    assert await state.get_outbox(status="sent") == []


@pytest.mark.asyncio
async def test_drain_gate_failure_marks_failed(state):
    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello", body="Unrendered {{first_name}} tag",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._drain_due()
    failed = await state.get_outbox(status="failed")
    assert len(failed) == 1
    assert "gate" in failed[0]["error"]
    assert provider.sent == []


@pytest.mark.asyncio
async def test_send_failure_recorded(state):
    pid = await seed_prospect(state)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello", body="Fine body. Question?",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    provider.fail_next = True
    sender = make_sender(state, provider)
    await sender._drain_due()
    failed = await state.get_outbox(status="failed")
    assert failed and "smtp boom" in failed[0]["error"]


# ── handler: stop-on-reply, bounces, kill switch ──


@pytest.mark.asyncio
async def test_reply_cancels_pending_outbox(state):
    pid = await seed_prospect(state, status="contacted")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2",
        body="b", send_at=_now_iso(), status="approved", campaign_id="c1", step=2,
    )
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="unsubscribe please",
    )]
    handler = make_handler(state, provider)
    await handler._run_native()

    cancelled = await state.get_outbox(status="cancelled")
    assert len(cancelled) == 1
    prospect = await state.get_prospect(pid)
    assert prospect.status == "opted_out"   # opt-out keywords honored

    # Taking the list to another sequencer must not undo the handled opt-out.
    from harvey.export import collect_prospects
    assert await collect_prospects(state) == []


async def _logged(state, action_type):
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(
            "SELECT details_json FROM actions WHERE action_type = ?", (action_type,)
        ) as cursor:
            return [json.loads(r[0]) for r in await cursor.fetchall()]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,intent,rule,matched", [
    ("Please remove me from your list.", "question", "phrase", "remove me"),
    ("No thanks, we're all set for now.", "question", "no_thanks", "no thanks"),
    ("STOP", "question", "only_words", "stop"),
    ("Please never write to this address again.", "unsubscribe", "model", ""),
    ("תורידו אותי מהרשימה.", "question", "phrase", "תורידו אותי"),
    ("לא מעוניין, תודה.", "question", "no_thanks", "לא מעוניין, תודה"),
    ("הסר", "question", "only_words", "הסר"),
])
async def test_an_opt_out_records_why(state, body, intent, rule, matched):
    pid = await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in4", from_email="jane@acme.com", subject="Re: hi", body=body,
    )]
    await make_handler(state, provider, intent=intent)._run_native()

    assert (await state.get_prospect(pid)).status == "opted_out"
    [d] = await _logged(state, "opt_out")
    assert d["prospect_id"] == pid and d["prospect_email"] == "jane@acme.com"
    assert (d["rule"], d["matched"]) == (rule, matched)
    assert d["message"] == body
    assert d["why"]
    [convo] = await state.get_conversations_by_status("closed")
    assert d["conversation_id"] == convo.id


class RefusingBrain(StubBrain):
    """Fails the test if anything asks the model about this reply."""
    async def think(self, *_a, **_k):
        raise AssertionError("no model call for someone who opted out")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Thanks, actually could you tell me more about pricing?",
    "I am out of the office until Monday.",
    "Not interested.",
])
async def test_reply_after_opt_out_keeps_them_opted_out(state, body):
    # An out-of-office or a later "thanks" used to set them to 'replied'
    # (or 'lost'), which put them back in the export, and a question got a
    # drafted answer in the Outbox.
    pid = await seed_prospect(state, status="opted_out")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in2", from_email="jane@acme.com", subject="Re: hi", body=body,
    )]
    handler = make_handler(state, provider)
    handler.brain = RefusingBrain()
    await handler._run_native()

    assert (await state.get_prospect(pid)).status == "opted_out"
    assert await state.get_outbox() == []
    from harvey.export import collect_prospects
    assert await collect_prospects(state) == []
    # Their message is still kept where a human can read it.
    convos = await state.get_conversations_by_status("closed")
    assert [c.prospect_id for c in convos] == [pid]
    assert convos[0].thread[-1].content.startswith(body.split(",")[0].split(".")[0])


@pytest.mark.asyncio
async def test_complaint_after_opt_out_still_reaches_a_human(state):
    pid = await seed_prospect(state, status="opted_out")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in3", from_email="jane@acme.com", subject="Re: hi",
        body="I told you to stop. I'm reporting you for spam.",
    )]
    handler = make_handler(state, provider)
    handler.brain = RefusingBrain()
    await handler._run_native()

    assert (await state.get_prospect(pid)).status == "opted_out"
    assert await state.get_outbox() == []
    flagged = await state.get_conversations_by_status("needs_human")
    assert [c.prospect_id for c in flagged] == [pid]
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT action_type FROM actions") as cursor:
            logged = [r[0] for r in await cursor.fetchall()]
    assert "escalation" in logged


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["replied", "lost", "contacted", "queued", "new"])
async def test_an_opt_out_is_never_overwritten(state, status):
    pid = await seed_prospect(state, status="opted_out")
    await state.update_prospect_status(pid, status)
    assert (await state.get_prospect(pid)).status == "opted_out"


@pytest.mark.asyncio
async def test_bounce_marks_invalid_and_kill_switch(state):
    pid = await seed_prospect(state, status="contacted")
    # A sent item whose message-id the bounce references
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s",
        body="b", send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    await state.update_outbox_item(
        item_id, status="sent", message_id="<m1@x>", sent_at=_now_iso()
    )
    # Ten more sent items so the kill-switch minimum-sample rule is met
    for i in range(10):
        oid = await state.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject="s", body="b",
            send_at=_now_iso(), status="approved", campaign_id=f"c{i+2}", step=1,
        )
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso())

    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body="couldn't be delivered", in_reply_to="<m1@x>", is_bounce=True,
    )]
    handler = make_handler(state, provider)
    await handler._run_native()

    prospect = await state.get_prospect(pid)
    assert prospect.email_status == "invalid"
    # 1 bounce / 11 sent = 9% > 5% threshold with >= 10 sends → kill switch
    assert await state.get_setting("sending_paused") != ""


@pytest.mark.asyncio
async def test_inbound_dedup(state):
    await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="we're all set thanks",
    )]
    handler = make_handler(state, provider, intent="not_interested")
    await handler._run_native()
    first = await state.get_conversations_by_status("closed")
    await handler._run_native()   # same message again → ignored via dedup
    second = await state.get_conversations_by_status("closed")
    assert len(first) == len(second) == 1


@pytest.mark.asyncio
async def test_after_an_undo_their_next_reply_is_handled_normally(state):
    pid = await seed_prospect(state, status="opted_out")
    assert await state.undo_opt_out(pid) is True
    assert await state.undo_opt_out(pid) is False       # only from opted_out
    assert (await state.get_prospect(pid)).status == "replied"

    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in5", from_email="jane@acme.com", subject="Re: hi",
        body="Sorry, wrong button earlier. What does it cost?",
    )]
    await make_handler(state, provider)._run_native()
    drafts = await state.get_outbox(status="pending_review")
    assert [d["kind"] for d in drafts] == ["reply"]      # waits for approval, as any reply


@pytest.mark.asyncio
async def test_on_autopilot_the_first_reply_after_an_undo_still_waits(state, monkeypatch):
    # The undo panel promises their next reply is drafted for approval.
    monkeypatch.setattr(Cfg.channels.email, "require_approval", False)
    pid = await seed_prospect(state, status="opted_out")
    assert await state.undo_opt_out(pid) is True

    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in6", from_email="jane@acme.com", subject="Re: hi",
        body="Sorry, wrong button. What does it cost?",
    )]
    await make_handler(state, provider)._run_native()
    assert [d["status"] for d in await state.get_outbox()] == ["pending_review"]

    # Only the first one: after that, autopilot is autopilot again.
    provider.inbound = [InboundMessage(
        provider_id="in7", from_email="jane@acme.com", subject="Re: hi",
        body="And is there a contract?",
    )]
    await make_handler(state, provider)._run_native()
    # (Their newer reply also cancels the draft still waiting, as always.)
    assert sorted(d["status"] for d in await state.get_outbox()) == ["approved", "cancelled"]


@pytest.mark.asyncio
async def test_on_instantly_the_first_reply_after_an_undo_goes_to_you(state, monkeypatch):
    import harvey.agents.handler as handler_mod
    monkeypatch.setattr(handler_mod, "legacy_sending_held", lambda cfg: False)
    pid = await seed_prospect(state, status="opted_out")
    assert await state.undo_opt_out(pid) is True

    handler = make_handler(state, FakeProvider())
    handler.provider = None                      # legacy Instantly path
    sent = []

    async def send_reply(uuid, body):
        sent.append(body)
        return {"ok": True}
    handler.instantly.send_reply = send_reply
    await handler._process_reply("jane@acme.com", "Sorry, wrong button. What does it cost?", "u1")

    assert sent == []
    assert [c.prospect_id for c in await state.get_conversations_by_status("needs_human")] == [pid]


@pytest.mark.asyncio
async def test_undo_and_its_log_are_one_write(state):
    pid = await seed_prospect(state, status="opted_out")
    assert await state.undo_opt_out(pid, details={"previous_why": "w"}) is True
    [d] = await _logged(state, "opt_out_undone")
    assert d == {"prospect_id": pid, "previous_why": "w"}
    assert await state.undo_opt_out(pid) is False
    assert len(await _logged(state, "opt_out_undone")) == 1     # no log for a refused undo


@pytest.mark.asyncio
async def test_actions_are_indexed_by_type(state):
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                              "AND tbl_name = 'actions'") as cursor:
            names = {r[0] for r in await cursor.fetchall()}
    assert "idx_actions_type_created" in names
