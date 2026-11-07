"""Several sending inboxes: storage, caps, connecting, routing, replies."""

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio

from sellbird import extensions, mailboxes
from sellbird.config import EmailChannelConfig, WarmupConfig
from sellbird.integrations.mail_provider import InboundMessage, MailProvider, SendResult
from sellbird.integrations.smtp_mail import SmtpImapProvider, SmtpSettings
from sellbird.mailboxes import MAIN, ConnectRefused, MailboxStore
from sellbird.secrets import MemorySecretBackend, SecretStore
from sellbird.state import StateManager

from tests.test_outbox_native import Cfg, Env, StubBrain, seed_campaign, seed_prospect


class Box(MailProvider):
    """A fake inbox that records what it sent."""

    def __init__(self, address, *, headers=True, configured=True):
        self.name = "fake"
        self.address = address
        self.carries_headers = headers
        self.configured = configured
        self.sent = []
        self.inbound = []
        self.read_fails = False
        self.confirmed = 0

    def is_configured(self):
        return self.configured

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", **_):
        self.sent.append({"to": to_email, "subject": subject, "in_reply_to": in_reply_to})
        return SendResult(ok=True, message_id=f"<{self.address}-{len(self.sent)}>", thread_ref="t")

    async def get_replies(self, limit=50):
        if self.read_fails:
            raise RuntimeError("imap down")
        return self.inbound

    def confirm_read(self):
        self.confirmed += 1

    async def test_connection(self):
        return True, f"signed in as {self.address}"


@pytest_asyncio.fixture
async def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = MailboxStore(os.path.join(tmp, "test.db"))
        await s.init_db()
        yield s


@pytest.fixture
def boxes(monkeypatch):
    """Extra inboxes connected as 'fake': each address gets one Box."""
    made: dict[str, Box] = {}
    monkeypatch.setattr(extensions, "_REGISTRY", extensions.Registry())
    monkeypatch.setattr(extensions, "_LOADED", True)

    def build(row, *, config, env, secrets):
        return made.setdefault(row["address"], Box(row["address"]))

    extensions._REGISTRY.add_mailbox_connection("fake", build)
    return made


async def add_inbox(store, address, **extra):
    mid = mailboxes._new_id()
    await store.add_mailbox(mailbox_id=mid, address=address, provider="fake",
                            connection="fake", **extra)
    return mid


def sender_for(state, main, *, approval=False):
    from sellbird.agents.sender import Sender
    cfg = Cfg()
    cfg.channels.email.require_approval = approval
    s = Sender(brain=None, state=state, config=cfg, env=Env())
    s.provider = main
    s.send_pacing = False
    return s


def handler_for(state, main):
    from sellbird.agents.handler import Handler
    h = Handler(brain=StubBrain(), state=state, config=Cfg(), env=Env())
    h.provider = main
    return h


# ── storage ──

@pytest.mark.asyncio
async def test_an_address_connects_once_and_comes_back_with_its_id(store):
    mid = await add_inbox(store, "A@Example.com")
    assert (await store.get_mailbox(mid))["address"] == "a@example.com"
    with pytest.raises(mailboxes.MailboxExists):
        await add_inbox(store, "a@example.com")
    assert await store.retire(mid)
    assert await store.list_mailboxes() == []
    # Connected again: the same row, active, its old pause and cap gone.
    again = await store.add_mailbox(mailbox_id=mid, address="a@example.com",
                                    provider="fake", connection="fake")
    row = await store.get_mailbox(again)
    assert again == mid and row["state"] == "active" and row["cap_override"] is None


@pytest.mark.asyncio
async def test_a_cap_must_be_a_sane_whole_number(store):
    mid = await add_inbox(store, "a@example.com")
    for bad in (0, -1, 501, True, 2.5):
        with pytest.raises(ValueError):
            await store.set_cap(mid, bad)
    assert await store.set_cap(mid, 40)
    assert await store.set_cap(mid, None)


def test_an_inbox_ramps_from_its_own_warmup_day():
    cfg = EmailChannelConfig(per_inbox_daily_cap=30,
                             warmup=WarmupConfig(min_days=14, start_sends=5, daily_increase=3))
    today = date(2026, 11, 7)
    assert mailboxes.inbox_cold_cap({}, cfg, today) == 30
    started = (today - timedelta(days=10)).isoformat()
    assert mailboxes.inbox_cold_cap({"warmup_started_at": started}, cfg, today) == 0
    started = (today - timedelta(days=16)).isoformat()
    assert mailboxes.inbox_cold_cap({"warmup_started_at": started}, cfg, today) == 11
    assert mailboxes.inbox_cold_cap({"cap_override": 80, "warmup_started_at": started},
                                    cfg, today) == 80


def test_per_inbox_cap_is_checked():
    with pytest.raises(ValueError):
        EmailChannelConfig(per_inbox_daily_cap=0)
    assert EmailChannelConfig().per_inbox_daily_cap == 30


# ── connecting ──

class Config:
    class persona:
        name = "Ethan"; email = "ethan@main.co"

    class channels:
        class email:
            provider = "smtp"


@pytest.mark.asyncio
async def test_smtp_keeps_its_password_in_the_keychain_only(store):
    backend = MemorySecretBackend()
    secrets = SecretStore(backend=backend, env={})
    mid = await mailboxes.connect_smtp_mailbox(
        store, secrets, Config, address="Two@Side.co", display_name="Ethan R",
        warmup_started_at="", settings=SmtpSettings(host="smtp.side.co", username="two@side.co",
                                                    password="hunter2"))
    row = await store.get_mailbox(mid)
    assert "hunter2" not in json.dumps(row)
    assert backend.get_password("sellbird", f"mailbox:{mid}:smtp") == "hunter2"
    provider = mailboxes.build_provider(row, config=Config, env=Env(), secrets=secrets)
    assert isinstance(provider, SmtpImapProvider)
    assert provider.smtp_pass == "hunter2" and provider.identity.address == "two@side.co"
    assert provider.identity.display_name == "Ethan R"


@pytest.mark.asyncio
async def test_an_oauth_sign_in_keeps_only_the_refresh_token(store):
    backend = MemorySecretBackend()
    secrets = SecretStore(backend=backend, env={})
    mid = await mailboxes.connect_oauth_mailbox(
        store, secrets, Config, provider="google", address="two@side.co", display_name="",
        warmup_started_at="2026-11-01",
        token={"refresh_token": "r", "access_token": "a" * 3000, "scope": "x"})
    stored = json.loads(backend.get_password("sellbird", f"mailbox:{mid}:oauth"))
    assert stored == {"refresh_token": "r"}
    assert (await store.get_mailbox(mid))["warmup_started_at"] == "2026-11-01"


@pytest.mark.asyncio
async def test_a_locked_keychain_connects_nothing(store):
    class Locked(MemorySecretBackend):
        def set_password(self, *a):
            import keyring.errors
            raise keyring.errors.KeyringError("locked")

    with pytest.raises(mailboxes.SecretStoreUnavailable):
        await mailboxes.connect_smtp_mailbox(
            store, SecretStore(backend=Locked(), env={}), Config, address="two@side.co",
            display_name="", warmup_started_at="",
            settings=SmtpSettings(host="h", username="u", password="p"))
    assert await store.list_mailboxes() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("address,message", [
    ("nope", "full email address"), ("ETHAN@main.co", "already your main mailbox")])
async def test_bad_addresses_are_refused(store, address, message):
    with pytest.raises(ConnectRefused, match=message):
        await mailboxes.connect_smtp_mailbox(
            store, SecretStore(backend=MemorySecretBackend(), env={}), Config, address=address,
            display_name="", warmup_started_at="",
            settings=SmtpSettings(host="h", username="u", password="p"))


def test_instantly_installs_connect_no_inbox():
    class Instantly(Config):
        class channels:
            class email:
                provider = "instantly"
    with pytest.raises(ConnectRefused, match="Instantly"):
        mailboxes.check_provider_allows_inboxes(Instantly)


def test_a_future_warmup_start_is_refused():
    with pytest.raises(ConnectRefused):
        mailboxes.check_warmup_start((date.today() + timedelta(days=5)).isoformat())
    assert mailboxes.check_warmup_start("") == ""


def test_built_in_connections_cant_be_replaced():
    reg = extensions.Registry()
    with pytest.raises(extensions.ExtensionError):
        reg.add_mailbox_connection("smtp", lambda *a, **k: None)


# ── one inbox: unchanged ──

@pytest.mark.asyncio
async def test_with_no_extra_inbox_nothing_is_assigned(store, boxes):
    main = Box("sellbird@x.co")
    pid = await seed_prospect(store)
    await seed_campaign(store, [pid])
    await sender_for(store, main)._run_native()
    assert len(main.sent) == 1
    assert {r["mailbox_id"] for r in await store.get_outbox()} == {""}


# ── several inboxes ──

@pytest.mark.asyncio
async def test_new_threads_spread_and_each_thread_keeps_its_inbox(store, boxes):
    main = Box("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")
    pids = [await seed_prospect(store, email=f"p{i}@acme{i}.com") for i in range(4)]
    await seed_campaign(store, pids)
    await sender_for(store, main)._run_native()

    assert len(main.sent) == 2 and len(boxes["two@side.co"].sent) == 2
    rows = await store.get_outbox()
    by_prospect = {}
    for r in rows:
        by_prospect.setdefault(r["prospect_id"], set()).add(r["mailbox_id"])
    # Both steps of every thread are on one inbox.
    assert all(len(ids) == 1 for ids in by_prospect.values())
    assert {r["mailbox_id"] for r in rows if r["status"] == "sent"} == {MAIN, side}
    sent_side = [r for r in rows if r["status"] == "sent" and r["mailbox_id"] == side]
    assert all(r["provider"] == "fake" for r in sent_side)


@pytest.mark.asyncio
async def test_a_follow_up_leaves_from_the_inbox_that_started_the_thread(store, boxes):
    main = Box("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")
    pid = await seed_prospect(store)
    campaign = await seed_campaign(store, [pid])
    await store.set_campaign_pool(campaign.id, [side])
    sender = sender_for(store, main)
    await sender._run_native()
    assert len(boxes["two@side.co"].sent) == 1 and main.sent == []

    # The follow-up is due; the campaign may now use every inbox, and the
    # side inbox is the fuller one, but the thread stays where it started.
    await store.set_campaign_pool(campaign.id, [])
    async with store._connect() as db:
        await db.execute("UPDATE outbox SET send_at = ? WHERE step = 2",
                         ((datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
                          .isoformat(),))
        await db.execute("UPDATE outbox SET sent_at = ? WHERE step = 1",
                         ((datetime.now(timezone.utc) - timedelta(days=4)).replace(tzinfo=None)
                          .isoformat(),))
        await db.commit()
    await sender._run_native()
    assert len(boxes["two@side.co"].sent) == 2 and main.sent == []


@pytest.mark.asyncio
async def test_each_inbox_stops_at_its_own_cold_cap(store, boxes, monkeypatch):
    # With several inboxes the main mailbox is one of them: per_inbox_daily_cap.
    monkeypatch.setattr(Cfg.channels.email, "per_inbox_daily_cap", 2, raising=False)
    main = Box("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")
    await store.set_cap(side, 1)
    pids = [await seed_prospect(store, email=f"p{i}@acme{i}.com") for i in range(5)]
    await seed_campaign(store, pids)
    await sender_for(store, main)._run_native()
    assert len(main.sent) == 2 and len(boxes["two@side.co"].sent) == 1
    # The other two wait for tomorrow's room on their own inbox.
    assert len(await store.get_outbox(status="approved", kind="sequence")) >= 2


@pytest.mark.asyncio
async def test_a_paused_inbox_hands_its_unsent_first_emails_on(store, boxes):
    main = Box("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")
    pid = await seed_prospect(store)
    campaign = await seed_campaign(store, [pid])
    await store.set_campaign_pool(campaign.id, [side])
    sender = sender_for(store, main, approval=True)
    await sender._run_native()                       # staged on the side inbox
    assert {r["mailbox_id"] for r in await store.get_outbox()} == {side}

    await store.set_pause(side, "testing")
    await store.set_campaign_pool(campaign.id, [])
    async with store._connect() as db:
        await db.execute("UPDATE outbox SET status = 'approved'")
        await db.commit()
    await sender._run_native()
    assert len(main.sent) == 1 and boxes["two@side.co"].sent == []
    assert {r["mailbox_id"] for r in await store.get_outbox()} == {MAIN}


@pytest.mark.asyncio
async def test_no_inbox_for_a_campaign_holds_its_email(store, boxes):
    main = Box("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")
    await store.set_pause(side, "testing")
    pid = await seed_prospect(store)
    campaign = await seed_campaign(store, [pid])
    await store.set_campaign_pool(campaign.id, [side])
    await sender_for(store, main)._run_native()
    assert main.sent == [] and boxes["two@side.co"].sent == []
    assert {r["status"] for r in await store.get_outbox()} == {"approved"}


@pytest.mark.asyncio
async def test_a_removed_inbox_ends_the_threads_it_started(store, boxes):
    main = Box("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")
    pid = await seed_prospect(store)
    campaign = await seed_campaign(store, [pid])
    await store.set_campaign_pool(campaign.id, [side])
    sender = sender_for(store, main)
    await sender._run_native()
    await store.retire(side)
    async with store._connect() as db:
        await db.execute("UPDATE outbox SET send_at = ? WHERE step = 2", ("2000-01-01",))
        await db.execute("UPDATE outbox SET sent_at = ? WHERE step = 1", ("2000-01-01",))
        await db.commit()
    await sender._run_native()
    step2 = next(r for r in await store.get_outbox() if r["step"] == 2)
    assert step2["status"] == "cancelled" and "removed" in step2["error"]
    assert main.sent == []


@pytest.mark.asyncio
async def test_headers_wait_for_an_inbox_that_can_carry_them(store, boxes, monkeypatch):
    main = Box("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")
    boxes["two@side.co"] = Box("two@side.co", headers=False)
    extensions._REGISTRY.add_send_headers("unsub", lambda item: [("List-Unsubscribe", "<x>")])
    pid = await seed_prospect(store)
    campaign = await seed_campaign(store, [pid])
    await store.set_campaign_pool(campaign.id, [side])
    await sender_for(store, main)._run_native()
    assert boxes["two@side.co"].sent == []
    step1 = next(r for r in await store.get_outbox() if r["step"] == 1)
    assert step1["status"] == "approved" and step1["error"].startswith("held:")


@pytest.mark.asyncio
async def test_a_reply_is_answered_from_the_inbox_it_arrived_in(store, boxes):
    main = Box("sellbird@x.co")
    side = await add_inbox(store, "two@side.co")
    pid = await seed_prospect(store, status="contacted")
    handler = handler_for(store, main)
    await handler._run_native()                           # builds the side Box
    boxes["two@side.co"].inbound = [InboundMessage(
        provider_id="r1", from_email="jane@acme.com", subject="hi",
        body="What does it cost?", message_id="<their@1>")]
    assert await handler._run_native() is True
    reply = next(r for r in await store.get_outbox() if r["kind"] == "reply")
    assert reply["mailbox_id"] == side
    convo = await store.get_conversations_by_status("open")
    assert await store.conversation_mailbox(convo[0].id) == side
    assert boxes["two@side.co"].confirmed >= 1

    async with store._connect() as db:
        await db.execute("UPDATE outbox SET status = 'approved' WHERE kind = 'reply'")
        await db.commit()
    await sender_for(store, main)._run_native()
    assert [m["in_reply_to"] for m in boxes["two@side.co"].sent] == ["<their@1>"]
    assert main.sent == []
    assert (await store.get_prospect(pid)).status == "replied"


@pytest.mark.asyncio
async def test_one_unreadable_inbox_holds_sending_but_the_others_are_read(store, boxes):
    main = Box("sellbird@x.co")
    await add_inbox(store, "two@side.co")
    await seed_prospect(store, status="contacted")
    handler = handler_for(store, main)
    await handler._run_native()
    boxes["two@side.co"].read_fails = True
    main.inbound = [InboundMessage(provider_id="r1", from_email="jane@acme.com",
                                   subject="hi", body="Not interested, thanks.",
                                   message_id="<their@1>")]
    assert await handler._run_native() is False
    assert "two@side.co" in handler.last_read_problem
    assert (await store.get_prospect_by_email("jane@acme.com")).status == "replied"


@pytest.mark.asyncio
async def test_inbox_providers_are_kept_while_their_row_is_unchanged(store, boxes):
    cache: dict = {}
    mid = await add_inbox(store, "two@side.co")
    first = await mailboxes.load_inboxes(store, Cfg(), Env(), None, cache=cache)
    again = await mailboxes.load_inboxes(store, Cfg(), Env(), None, cache=cache)
    assert first.get(mid).provider is again.get(mid).provider
    await store.retire(mid)
    await mailboxes.load_inboxes(store, Cfg(), Env(), None, cache=cache)
    assert cache == {}


# ── the command line ──

@pytest.mark.asyncio
async def test_the_cli_pauses_caps_and_lists(store, boxes, capsys):
    from argparse import Namespace
    from sellbird import inbox_cli
    await add_inbox(store, "two@side.co")
    secrets = SecretStore(backend=MemorySecretBackend(), env={})
    cfg = Cfg()
    cfg.channels.email.per_inbox_daily_cap = 30
    assert await inbox_cli.run(Namespace(inboxes_action="cap", address="two@side.co", cap=12),
                               store, secrets, cfg, Env()) == 0
    assert await inbox_cli.run(Namespace(inboxes_action="pause", address="two@side.co",
                                         reason="holiday"), store, secrets, cfg, Env()) == 0
    capsys.readouterr()
    assert await inbox_cli.run(Namespace(inboxes_action="list"), store, secrets, cfg, Env()) == 0
    out = capsys.readouterr().out
    assert "two@side.co" in out and "paused" in out and "0/12" in out and "holiday" in out
    assert await inbox_cli.run(Namespace(inboxes_action="test", address="nobody@x.co"),
                               store, secrets, cfg, Env()) == 1
