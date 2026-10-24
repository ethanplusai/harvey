"""End-to-end: the real heartbeat, agents, Brain and Codex adapter, driven
through the scripted Codex CLI (tests/fake_codex.py).

Everything outside Harvey is a fixture: a disposable database and config,
a capturing mail transport (nothing is ever sent over the network), a
stubbed Scout and profiler (they would reach the internet), and a fake
ChatGPT login. These tests check what an operator would see across whole
heartbeat cycles: what is queued or sent, what is consumed, what is
counted, and what happens when Codex can't be used.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

import harvey.brain as brain_mod
import harvey.codex as hc

# The Codex provider supports macOS and Linux only (see harvey.codex).
pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="the Codex provider isn't supported on Windows")
import harvey.main as main_mod
from harvey.config import EnvConfig, HarveyConfig
from harvey.integrations.mail_provider import InboundMessage
from harvey.models.conversation import Conversation, Message
from harvey.state import StateManager
from tests.test_codex import _write_login
from tests.test_outbox_native import FakeProvider, _now_iso, seed_prospect

FAKE = str(Path(__file__).with_name("fake_codex.py"))
FIXTURES = Path(__file__).with_name("fixtures")

BASE = {
    "persona": {"name": "Harvey", "company": "Acme Tools", "role": "BD",
                "email": "harvey@acme.example", "linkedin": "", "tone": "direct"},
    "product": {"name": "Widget", "description": "Widgets for teams", "pricing": "$10",
                "key_benefits": ["fast"], "objection_responses": {}},
    "icp": {"industries": ["software"], "company_size": "10-50", "titles": ["VP"],
            "geography": ["US"]},
    "channels": {"email": {"provider": "smtp", "require_approval": True}},
}

FAKE_CLAUDE = (
    "import json, sys; sys.stdin.read() if not sys.stdin.isatty() else None; "
    "print(json.dumps({'type': 'result', 'result': 'question', 'session_id': 's',"
    " 'usage': {'input_tokens': 3, 'output_tokens': 1}}))"
)


class Harness:
    """One disposable Harvey install wired to fixtures."""

    def __init__(self, monkeypatch, tmp_path, provider="codex", model="gpt-5.5", db=None):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.log = tmp_path / "codex.log"
        self.scenario = "smart"
        self.native = tmp_path / "native-codex"
        self.native.mkdir(exist_ok=True)
        _write_login(self.native)
        self.state = StateManager(str(db or tmp_path / "harvey.db"))
        self.mail = FakeProvider()
        self.cycles = 0
        self.stop_after = 1
        self.between = {}
        self.scout_runs = 0
        self.set_config(provider, model)

        runtimes = tmp_path / "runtimes"
        runtimes.mkdir(exist_ok=True)
        monkeypatch.setattr(tempfile, "tempdir", str(runtimes))
        monkeypatch.setattr(hc, "find_codex", lambda: "codex")
        monkeypatch.setattr(hc, "native_codex_home", lambda: self.native)
        monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)
        real_exec = asyncio.create_subprocess_exec

        async def exec_routed(program, *args, **kwargs):
            if program == "codex":
                return await real_exec(sys.executable, FAKE, self.scenario, str(self.log),
                                       *args, **kwargs)
            if program == "claude":
                return await real_exec(sys.executable, "-c", FAKE_CLAUDE, **kwargs)
            raise AssertionError(f"unexpected subprocess {program!r}")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_routed)

        import harvey.agents.handler as handler_mod
        import harvey.agents.scout as scout_mod
        import harvey.agents.sender as sender_mod

        monkeypatch.setattr(handler_mod, "get_mail_provider", lambda c, e: self.mail)
        monkeypatch.setattr(sender_mod, "get_mail_provider", lambda c, e: self.mail)

        async def scout_run(_self):
            self.scout_runs += 1

        async def no_profile(state, limit=25, **_):
            return None

        monkeypatch.setattr(scout_mod.Scout, "run", scout_run)
        monkeypatch.setattr(main_mod, "run_profile_stage", no_profile)
        monkeypatch.setattr(main_mod, "load_config", lambda: self.config)
        monkeypatch.setattr(main_mod, "load_env", lambda: EnvConfig())
        monkeypatch.setattr(main_mod, "StateManager", lambda: self.state)
        monkeypatch.setattr(main_mod, "in_quiet_hours", lambda config: False)

        async def one_cycle_sleep(seconds, stop_event):
            self.cycles += 1
            step = self.between.get(self.cycles)
            if step is not None:
                result = step()
                if asyncio.iscoroutine(result):
                    await result
            return self.cycles >= self.stop_after

        monkeypatch.setattr(main_mod, "_interruptible_sleep", one_cycle_sleep)
        # Sender: no inter-send pacing sleeps.
        real_init = sender_mod.Sender.__init__

        def init_no_pacing(s, *a, **k):
            real_init(s, *a, **k)
            s.send_pacing = False

        monkeypatch.setattr(sender_mod.Sender, "__init__", init_no_pacing)

    def set_config(self, provider, model=""):
        brain = {"provider": provider}
        if model:
            brain["model"] = model
        self.config = HarveyConfig(**BASE, brain=brain)

    async def run_cycles(self, n=1, between=None):
        """Run one heartbeat (one Harvey process) for ``n`` cycles.
        ``between[k]`` runs after cycle k, inside the same process."""
        self.cycles = 0
        self.stop_after = n
        self.between = between or {}
        await asyncio.wait_for(main_mod.heartbeat(asyncio.Event()), 120)

    def entries(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def count(self, key, value):
        return sum(1 for e in self.entries() if e.get(key) == value)

    async def status(self):
        return brain_mod.parse_brain_status(await self.state.get_setting(brain_mod.BRAIN_STATUS_KEY))

    def fail_from_call(self, n, scenario="hooks"):
        """Switch the CLI to ``scenario`` at the n-th model call (preflight
        checks aren't model calls), e.g. to fail the reply draft only. It
        stays failing until ``recover()``."""
        real_run = hc.CodexClient.run
        calls = {"n": 0}

        async def run(client, prompt, timeout, skip_if=None):
            calls["n"] += 1
            if calls["n"] == n:
                self.scenario = scenario
            return await real_run(client, prompt, timeout, skip_if=skip_if)

        self.monkeypatch.setattr(hc.CodexClient, "run", run)
        return calls

    def recover(self):
        """The operator fixes Codex; the paused Brain re-checks next cycle."""
        self.scenario = "smart"
        self.monkeypatch.setattr(brain_mod, "PROVIDER_RECHECK_SECONDS", 0)


@pytest.fixture
def harvey(monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


async def _open_thread(state, email="jane@acme.com"):
    pid = await seed_prospect(state, email=email, status="contacted")
    convo = Conversation(id="", prospect_id=pid, channel="email",
                         thread=[Message(sender="harvey", content="Hi Jane, quick question?")])
    convo.id = await state.add_conversation(convo)
    return pid, convo.id


def _reply(body, provider_id="in-1", email="jane@acme.com"):
    return InboundMessage(provider_id=provider_id, from_email=email,
                          subject="Re: quick question", body=body, message_id=f"<{provider_id}@x>")


async def _approved_item(state, email="bob@beta.com"):
    pid = await seed_prospect(state, email=email, status="contacted")
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="following up",
        body="Hi Bob, any thoughts on this?", send_at=_now_iso(), status="approved",
        campaign_id="c-1", step=2,
    )


# ── healthy operation ──


@pytest.mark.asyncio
async def test_reply_is_classified_answered_for_review_and_never_reprocessed(harvey):
    await harvey.state.init_db()
    pid, convo_id = await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Thanks. Could you share pricing details?")]

    await harvey.run_cycles(2)  # the second cycle sees the same inbound message

    convo = await harvey.state.get_conversation(convo_id)
    assert convo.intent == "interested"
    assert [m.sender for m in convo.thread] == ["harvey", "prospect"]
    replies = await harvey.state.get_outbox(status="pending_review")
    assert len(replies) == 1 and replies[0]["kind"] == "reply"
    assert replies[0]["body"].startswith("Happy to help")
    assert harvey.mail.sent == []  # approval required: nothing leaves
    assert await harvey.state.is_reply_processed("in-1")
    # Exactly two model calls (classify + draft), counted once, on Codex only.
    assert harvey.count("cmd", "app-server") >= 2
    assert sum(1 for e in harvey.entries() if "prompt_chars" in e) == 2
    assert await harvey.state.get_usage_today("codex") == 2
    assert await harvey.state.get_usage_today("claude") == 0
    rows = await harvey.state.usage_by_provider(days=1)
    assert [(r["provider"], r["calls"]) for r in rows] == [("codex", 2)]
    assert (await harvey.status()) == {}  # never paused


@pytest.mark.asyncio
async def test_approved_email_is_delivered_once_across_cycles(harvey):
    await harvey.state.init_db()
    await _approved_item(harvey.state)
    await harvey.run_cycles(3)
    assert [m["to"] for m in harvey.mail.sent] == ["bob@beta.com"]
    assert len(await harvey.state.get_outbox(status="sent")) == 1
    assert await harvey.state.get_usage_today("codex") == 0  # sending needs no model


@pytest.mark.asyncio
async def test_writer_drafts_through_codex_and_waits_for_approval(harvey):
    await harvey.state.init_db()
    await seed_prospect(harvey.state, email="jane@acme.com", status="new")
    # One cycle: the writer drafts, then (send phase, after it) the sender
    # stages the drafts for approval. Nothing is sent.
    await harvey.run_cycles(1)
    drafts = await harvey.state.get_outbox(status="pending_review")
    assert drafts and all(d["to_email"] == "jane@acme.com" for d in drafts)
    assert harvey.mail.sent == []
    assert await harvey.state.get_usage_today("codex") >= 1
    assert harvey.scout_runs == 0


# ── the provider can't be used: pause, touch nothing, recover ──


@pytest.mark.asyncio
async def test_expired_login_pauses_everything_and_recovers_after_renewal(harvey):
    await harvey.state.init_db()
    _write_login(harvey.native, expires_in=-60)
    auth = harvey.native / "auth.json"
    _, convo_id = await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    await _approved_item(harvey.state)

    await harvey.run_cycles(2)
    # Paused before any agent ran: no reply consumed, no email sent, no
    # runtime started, and the reason is recorded for `harvey status`.
    assert not await harvey.state.is_reply_processed("in-1")
    assert harvey.mail.sent == []
    assert harvey.count("cmd", "app-server") == 0
    status = await harvey.status()
    assert status["status"] == "paused" and "expired" in status["reason"]
    assert len((await harvey.state.get_conversation(convo_id)).thread) == 1

    before = auth.read_bytes()
    _write_login(harvey.native)  # the operator runs `codex login`
    renewed = auth.read_bytes()
    await harvey.run_cycles(1)
    assert await harvey.state.is_reply_processed("in-1")
    assert [m["to"] for m in harvey.mail.sent] == ["bob@beta.com"]
    assert (await harvey.status())["status"] == "ok"
    assert renewed != before and auth.read_bytes() == renewed  # Harvey never wrote it


@pytest.mark.asyncio
async def test_renewed_login_is_picked_up_without_a_restart(harvey):
    await harvey.state.init_db()
    _write_login(harvey.native, refreshed_ago=9 * 86400)  # due for renewal
    await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    seen = {}

    async def still_paused():
        seen["processed"] = await harvey.state.is_reply_processed("in-1")
        seen["launched"] = harvey.count("cmd", "app-server")

    def renew():
        _write_login(harvey.native)  # `codex login`, or the operator using Codex

    await harvey.run_cycles(3, between={1: still_paused, 2: renew})
    assert seen == {"processed": False, "launched": 0}
    assert await harvey.state.is_reply_processed("in-1")
    assert (await harvey.status())["status"] == "ok"


@pytest.mark.asyncio
async def test_isolation_failure_mid_cycle_leaves_the_reply_for_later(harvey, monkeypatch):
    await harvey.state.init_db()
    _, convo_id = await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?"),
                           _reply("Another question", provider_id="in-2")]
    harvey.scenario = "hooks"  # credential-free checks pass; the call's checks don't
    seen = {}

    def after_first():
        seen["launched"] = harvey.count("cmd", "app-server")

    def after_second():
        seen["launched_after_second"] = harvey.count("cmd", "app-server")
        # The operator fixes the Codex config; the periodic re-check resumes.
        harvey.scenario = "smart"
        monkeypatch.setattr(brain_mod, "PROVIDER_RECHECK_SECONDS", 0)

    async def check_paused():
        assert not await harvey.state.is_reply_processed("in-1")
        assert not await harvey.state.is_reply_processed("in-2")
        convo = await harvey.state.get_conversation(convo_id)
        assert convo.status == "open" and len(convo.thread) == 1  # not escalated
        assert await harvey.state.get_outbox() == []
        assert (await harvey.status())["status"] == "paused"
        assert "turn/start" not in [e.get("method") for e in harvey.entries()]
        after_first()

    await harvey.run_cycles(3, between={1: check_paused, 2: after_second})
    # Cycle 2 stayed paused without starting a runtime.
    assert seen["launched_after_second"] == seen["launched"]
    # Cycle 3 resumed and handled both replies exactly once.
    assert await harvey.state.is_reply_processed("in-1")
    assert await harvey.state.is_reply_processed("in-2")
    assert (await harvey.status())["status"] == "ok"


@pytest.mark.asyncio
async def test_rejected_model_pauses_with_an_actionable_reason(monkeypatch, tmp_path):
    harvey = Harness(monkeypatch, tmp_path, model="missing-model")  # absent from catalog
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    await harvey.run_cycles(1)
    status = await harvey.status()
    assert status["status"] == "paused" and "missing-model" in status["reason"]
    assert not await harvey.state.is_reply_processed("in-1")
    assert sum(1 for e in harvey.entries() if "prompt_chars" in e) == 0


@pytest.mark.asyncio
async def test_usage_limit_stops_calls_and_closes_the_budget(harvey):
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    harvey.scenario = "usage_limit"
    turns = {}

    def after_first():
        turns["first"] = sum(1 for e in harvey.entries() if e.get("method") == "turn/start")

    await harvey.run_cycles(3, between={1: after_first})
    assert turns["first"] == 1  # one refused turn, not one per reply or retry
    # The next cycles in the same process keep the budget gate closed.
    assert sum(1 for e in harvey.entries() if e.get("method") == "turn/start") == 1
    assert not await harvey.state.is_reply_processed("in-1")


@pytest.mark.asyncio
async def test_restart_clears_a_stale_paused_status(harvey):
    """A pause recorded before a restart must not keep showing once healthy."""
    await harvey.state.init_db()
    await harvey.state.set_setting(brain_mod.BRAIN_STATUS_KEY, json.dumps(
        {"provider": "codex", "status": "paused", "reason": "old", "since": "x"}))
    await harvey.run_cycles(1)
    assert (await harvey.status())["status"] == "ok"


@pytest.mark.asyncio
async def test_transient_failure_skips_a_cycle_then_recovers(harvey):
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    harvey.scenario = "crash"  # the CLI dies mid-protocol
    await harvey.run_cycles(1)
    assert not await harvey.state.is_reply_processed("in-1")
    assert (await harvey.status()) == {}  # transient: not recorded as paused

    harvey.scenario = "smart"
    await harvey.run_cycles(1)
    assert await harvey.state.is_reply_processed("in-1")
    assert len(await harvey.state.get_outbox(status="pending_review")) == 1


@pytest.mark.asyncio
async def test_no_final_answer_escalates_to_a_human_without_replying(harvey):
    """A healthy provider that returns no usable answer is the existing
    Handler contract: flag for a human, never auto-reply blind."""
    await harvey.state.init_db()
    _, convo_id = await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    harvey.scenario = "commentary_only"
    await harvey.run_cycles(1)
    convo = await harvey.state.get_conversation(convo_id)
    assert convo.status == "needs_human"
    assert await harvey.state.get_outbox() == [] and harvey.mail.sent == []
    assert await harvey.state.get_usage_today("codex") == 0  # failed calls aren't counted


# ── existing installs and switching providers ──


def _load_pre_codex_db(path: Path):
    """A database written by Harvey 0.2.1 (before the Codex provider)."""
    con = sqlite3.connect(path)
    con.executescript((FIXTURES / "pre_codex_harvey_0_2_1.sql").read_text())
    con.close()


@pytest.mark.asyncio
async def test_pre_codex_database_migrates_and_runs_on_codex(monkeypatch, tmp_path):
    db = tmp_path / "old.db"
    _load_pre_codex_db(db)
    harvey = Harness(monkeypatch, tmp_path, db=db)
    await harvey.state.init_db()

    rows = await harvey.state.usage_by_provider(days=36500)
    assert [(r["provider"], r["calls"]) for r in rows] == [("claude", 1)]
    old_prospect = await harvey.state.get_prospect_by_email("old@legacy.com")
    assert old_prospect is not None and old_prospect.status == "contacted"

    _, convo_id = await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    await harvey.run_cycles(1)
    assert await harvey.state.is_reply_processed("in-1")
    rows = {r["provider"]: r["calls"] for r in await harvey.state.usage_by_provider(days=36500)}
    assert rows == {"claude": 1, "codex": 2}


@pytest.mark.asyncio
async def test_switching_providers_keeps_counters_and_budgets_separate(monkeypatch, tmp_path):
    harvey = Harness(monkeypatch, tmp_path, provider="claude", model="")
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    await harvey.run_cycles(1)
    # Claude default path: no Codex process at all, counted as Claude.
    assert harvey.entries() == []
    assert await harvey.state.get_usage_today("claude") >= 1
    assert await harvey.state.get_usage_today("codex") == 0
    claude_calls = await harvey.state.get_usage_today("claude")

    harvey.set_config("codex", "gpt-5.5")
    harvey.mail.inbound = [_reply("Could you share pricing details?", provider_id="in-2")]
    await harvey.run_cycles(1)
    assert await harvey.state.get_usage_today("codex") == 2
    assert await harvey.state.get_usage_today("claude") == claude_calls

    # The Codex budget counts Codex calls only.
    from harvey.brain import Brain
    brain = Brain(harvey.state, harvey.config.brain)
    brain.quota.get_utilization = _none
    assert await brain.is_within_budget(3, max_percent=None) is True
    assert await brain.is_within_budget(2, max_percent=None) is False


async def _none():
    return None


@pytest.mark.asyncio
async def test_fixture_transport_is_the_only_mail_path(harvey):
    """Guard for the tests above: no real SMTP/IMAP/API client is built."""
    await harvey.state.init_db()
    import smtplib
    import imaplib

    def refuse(*a, **k):
        raise AssertionError("a real mail connection was attempted")

    harvey.monkeypatch.setattr(smtplib, "SMTP", refuse)
    harvey.monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    harvey.monkeypatch.setattr(imaplib, "IMAP4_SSL", refuse)
    await _approved_item(harvey.state)
    await harvey.run_cycles(1)
    assert len(harvey.mail.sent) == 1


class _DownBrain:
    """A brain whose provider just became unusable mid-cycle."""
    paused_reason = "Your Codex login has expired or is due for renewal."

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, *a, **k):
        return ""


@pytest.mark.asyncio
async def test_instantly_path_leaves_the_reply_unprocessed_when_the_brain_is_down(tmp_path):
    """Legacy Instantly replies get the same protection as native ones."""
    from harvey.agents.handler import Handler
    from harvey.brain import BrainUnavailable
    from harvey.models.campaign import Campaign

    state = StateManager(str(tmp_path / "i.db"))
    await state.init_db()
    _, convo_id = await _open_thread(state)
    config = HarveyConfig(**{**BASE, "channels": {"email": {"provider": "instantly"}}})
    handler = Handler(brain=_DownBrain(), state=state, config=config, env=EnvConfig())
    campaign = Campaign(id="c1", name="c", channel="email", sequence=[], prospect_ids=[],
                        status="active", instantly_campaign_id="ic-1")

    class Instantly:
        async def get_replies(self, campaign_id):
            return [{"uuid": "r-1", "lead_email": "jane@acme.com",
                     "body": "Could you share pricing details?"}]

        async def send_reply(self, *a):
            raise AssertionError("must not reply")

    handler.instantly = Instantly()
    with pytest.raises(BrainUnavailable):
        await handler._handle_reply({"uuid": "r-1", "lead_email": "jane@acme.com",
                                     "body": "Could you share pricing details?"}, campaign)
    assert not await state.is_reply_processed("r-1")
    convo = await state.get_conversation(convo_id)
    assert convo.status == "open" and len(convo.thread) == 1

    async def active(status):
        return [campaign]

    state.get_campaigns_by_status = active
    await handler.run()  # the loop stops cleanly instead of escalating
    assert not await state.is_reply_processed("r-1")


# ── outages part-way through a reply, and sends waiting on replies ──


@pytest.mark.asyncio
async def test_outage_while_drafting_the_reply_loses_nothing_and_duplicates_nothing(harvey):
    """Classification succeeds, the provider fails before the reply draft:
    nothing about the reply may be stored, and the retry after recovery
    must leave exactly one inbound message, one stage change, one reply."""
    await harvey.state.init_db()
    _, convo_id = await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    harvey.fail_from_call(2)
    seen = {}

    async def after_outage():
        convo = await harvey.state.get_conversation(convo_id)
        seen.update(processed=await harvey.state.is_reply_processed("in-1"),
                    thread=[m.sender for m in convo.thread], stage=convo.stage,
                    status=convo.status, outbox=len(await harvey.state.get_outbox()),
                    brain=(await harvey.status()).get("kind"))
        harvey.recover()

    await harvey.run_cycles(3, between={1: after_outage})
    assert seen == {"processed": False, "thread": ["harvey"], "stage": "initial_outreach",
                    "status": "open", "outbox": 0, "brain": "isolation"}
    convo = await harvey.state.get_conversation(convo_id)
    assert [m.sender for m in convo.thread] == ["harvey", "prospect"]
    assert convo.stage == "engaged" and convo.intent == "interested"
    replies = await harvey.state.get_outbox(status="pending_review")
    assert len(replies) == 1 and replies[0]["kind"] == "reply"
    assert await harvey.state.is_reply_processed("in-1")
    assert (await harvey.status())["status"] == "ok"


@pytest.mark.asyncio
async def test_instantly_reply_survives_an_outage_while_drafting(harvey):
    """Legacy Instantly path: same guarantee, and the reply is sent once."""
    from harvey.agents.handler import Handler
    from harvey.brain import Brain
    from harvey.models.campaign import Campaign

    await harvey.state.init_db()
    _, convo_id = await _open_thread(harvey.state)
    config = HarveyConfig(**{**BASE, "channels": {"email": {
                                "provider": "instantly", "require_approval": False}},
                             "brain": {"provider": "codex", "model": "gpt-5.5"}})
    brain = Brain(harvey.state, config.brain)
    handler = Handler(brain=brain, state=harvey.state, config=config, env=EnvConfig())
    campaign = Campaign(id="c1", name="c", channel="email", sequence=[], prospect_ids=[],
                        status="active", instantly_campaign_id="ic-1")
    sent = []

    class Instantly:
        async def get_replies(self, campaign_id):
            return [{"uuid": "r-1", "lead_email": "jane@acme.com",
                     "body": "Could you share pricing details?"}]

        async def send_reply(self, reply_uuid, body):
            sent.append((reply_uuid, body))
            return {"ok": True}

    async def active(status):
        return [campaign]

    handler.instantly = Instantly()
    harvey.state.get_campaigns_by_status = active
    harvey.fail_from_call(2)
    assert await brain.preflight() == ""
    await handler.run()
    convo = await harvey.state.get_conversation(convo_id)
    assert (sent, len(convo.thread), convo.stage) == ([], 1, "initial_outreach")
    assert not await harvey.state.is_reply_processed("r-1")

    harvey.recover()
    assert await brain.preflight() == ""
    await handler.run()
    await handler.run()  # already processed: no second reply
    convo = await harvey.state.get_conversation(convo_id)
    assert [m.sender for m in convo.thread] == ["harvey", "prospect", "harvey"]
    assert convo.stage == "engaged" and len(sent) == 1 and sent[0][0] == "r-1"


@pytest.mark.asyncio
async def test_rejected_model_never_sends_queued_mail(monkeypatch, tmp_path):
    harvey = Harness(monkeypatch, tmp_path, model="missing-model")  # absent from catalog
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    await _approved_item(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    await harvey.run_cycles(1)
    assert harvey.mail.sent == []
    status = await harvey.status()
    assert status["status"] == "paused" and status["kind"] == "isolation"
    assert "missing-model" in status["reason"]
    assert not await harvey.state.is_reply_processed("in-1")
    assert not [e for e in harvey.entries() if "prompt_chars" in e]
    # Caught by the heartbeat preflight, before any agent ran: the mailbox
    # wasn't even read (the prospect isn't marked as having replied).
    jane = await harvey.state.get_prospect_by_email("jane@acme.com")
    assert jane.status == "contacted"


@pytest.mark.asyncio
async def test_outage_during_the_handler_holds_queued_sends_until_recovery(harvey):
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    await _approved_item(harvey.state)  # due to bob
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    harvey.fail_from_call(1)  # preflight passes; the classification call fails
    seen = {}

    async def after_outage():
        seen["sent"] = list(harvey.mail.sent)
        seen["processed"] = await harvey.state.is_reply_processed("in-1")
        harvey.recover()

    await harvey.run_cycles(3, between={1: after_outage})
    assert seen == {"sent": [], "processed": False}
    assert [m["to"] for m in harvey.mail.sent] == ["bob@beta.com"]  # once, after recovery
    assert await harvey.state.is_reply_processed("in-1")


@pytest.mark.asyncio
async def test_a_reply_cancels_its_due_follow_up_before_the_send_phase(harvey):
    """Stop-on-reply beats the outbox drain in the same cycle, even when the
    reply starts a new conversation (none was open before)."""
    await harvey.state.init_db()
    pid = await seed_prospect(harvey.state, email="jane@acme.com", status="contacted")
    await harvey.state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="following up",
        body="Hi Jane, any thoughts?", send_at=_now_iso(), status="approved",
        campaign_id="c-1", step=2)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    await harvey.run_cycles(1)
    assert harvey.mail.sent == []
    assert len(await harvey.state.get_outbox(status="cancelled")) == 1
    assert len(await harvey.state.get_outbox(status="pending_review")) == 1  # Harvey's answer
    assert await harvey.state.is_reply_processed("in-1")


# ── status truthfulness ──


@pytest.mark.asyncio
async def test_usage_limit_pause_is_recorded_survives_restart_and_clears(harvey, monkeypatch):
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    harvey.scenario = "usage_limit"
    await harvey.run_cycles(1)
    status = await harvey.status()
    assert status["status"] == "paused" and status["kind"] == "usage_limit"
    assert status["until"] > __import__("time").time()

    # Restart inside the pause: no new turn, still paused.
    turns = sum(1 for e in harvey.entries() if e.get("method") == "turn/start")
    harvey.scenario = "smart"
    await harvey.run_cycles(1)
    assert sum(1 for e in harvey.entries() if e.get("method") == "turn/start") == turns
    assert (await harvey.status())["kind"] == "usage_limit"
    assert not await harvey.state.is_reply_processed("in-1")

    # The pause runs out: recorded as recovered, and the reply is handled.
    stored = await harvey.status()
    stored["until"] = __import__("time").time() - 1
    await harvey.state.set_setting(brain_mod.BRAIN_STATUS_KEY, json.dumps(stored))
    await harvey.run_cycles(1)
    assert (await harvey.status())["status"] == "ok"
    assert await harvey.state.is_reply_processed("in-1")


@pytest.mark.asyncio
async def test_usage_limit_pause_expiry_is_recorded_in_process(harvey, monkeypatch):
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    harvey.scenario = "usage_limit"
    monkeypatch.setattr(brain_mod, "USAGE_LIMIT_PAUSE_SECONDS", 0.2)
    seen = {}

    async def after_limit():
        seen["kind"] = (await harvey.status()).get("kind")
        harvey.scenario = "smart"
        await asyncio.sleep(0.3)

    await harvey.run_cycles(2, between={1: after_limit})
    assert seen["kind"] == "usage_limit"
    assert (await harvey.status())["status"] == "ok"
    assert await harvey.state.is_reply_processed("in-1")


# ── sending waits for a completed mailbox phase (both providers) ──


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, model", [("codex", "gpt-5.5"), ("claude", "")])
async def test_mailbox_error_holds_sends_until_replies_can_be_read(monkeypatch, tmp_path,
                                                                   provider, model):
    harvey = Harness(monkeypatch, tmp_path, provider=provider, model=model)
    await harvey.state.init_db()
    await _approved_item(harvey.state)
    real_get_replies = harvey.mail.get_replies

    async def mailbox_down(limit=50):
        raise OSError("synthetic mailbox unavailable")

    harvey.mail.get_replies = mailbox_down
    seen = {}

    def after_failure():
        seen["sent"] = list(harvey.mail.sent)
        harvey.mail.get_replies = real_get_replies  # the mailbox is back

    await harvey.run_cycles(3, between={1: after_failure})
    assert seen["sent"] == []
    assert [m["to"] for m in harvey.mail.sent] == ["bob@beta.com"]  # once, after recovery
    assert len(await harvey.state.get_outbox(status="sent")) == 1


@pytest.mark.asyncio
async def test_timed_out_reply_handling_holds_sends(harvey, monkeypatch):
    await harvey.state.init_db()
    await _approved_item(harvey.state)
    real_get_replies = harvey.mail.get_replies

    async def mailbox_hangs(limit=50):
        await asyncio.sleep(30)
        return []

    harvey.mail.get_replies = mailbox_hangs
    monkeypatch.setattr(main_mod, "CYCLE_TIMEOUT_SECONDS", 0.5)
    seen = {}

    def after_timeout():
        seen["sent"] = list(harvey.mail.sent)
        harvey.mail.get_replies = real_get_replies
        monkeypatch.setattr(main_mod, "CYCLE_TIMEOUT_SECONDS", 600)

    await harvey.run_cycles(2, between={1: after_timeout})
    assert seen["sent"] == []
    assert [m["to"] for m in harvey.mail.sent] == ["bob@beta.com"]


@pytest.mark.asyncio
async def test_a_message_that_fails_to_process_holds_that_cycles_sends(harvey, monkeypatch):
    """An error handling one reply (here: storing it) means stop-on-reply may
    not have run for it; the cycle sends nothing, the next one does."""
    await harvey.state.init_db()
    await _open_thread(harvey.state)
    await _approved_item(harvey.state)
    harvey.mail.inbound = [_reply("Could you share pricing details?")]
    real_update = harvey.state.update_conversation
    calls = {"n": 0}

    async def flaky_update(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic database hiccup")
        return await real_update(*a, **k)

    harvey.state.update_conversation = flaky_update
    seen = {}

    def after_first():
        seen["sent"] = list(harvey.mail.sent)

    await harvey.run_cycles(2, between={1: after_first})
    assert seen["sent"] == []
    assert [m["to"] for m in harvey.mail.sent] == ["bob@beta.com"]
