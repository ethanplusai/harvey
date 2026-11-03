"""A batch the model can't write for must not loop forever.

When _write_sequence comes back empty (unparseable output, or an inbox
sequence that used a person's name), the batch's prospects stayed 'new', so
every heartbeat chose write_campaign again, spent a Claude call, and Scout
never ran. Now each prospect gets a few attempts; after that it is moved to
'write_failed', which is visible and no longer blocks the pipeline. A
provider outage is not the model's fault and never counts as an attempt.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.agents.writer import MAX_WRITE_ATTEMPTS, Writer
from sellbird.main import decide_next_action
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

GOOD = [{"step": 1, "subject": "quick one", "body": "Hi {{first_name}}, who answers at {{company}}?",
         "delay_days": 0}]


class FakeBrain:
    def __init__(self, reply=None, paused=""):
        self.reply = reply
        self.paused_reason = paused
        self.calls = 0

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.calls += 1
        return None if self.paused_reason else self.reply


CONFIG = SimpleNamespace(
    channels=SimpleNamespace(email=SimpleNamespace(provider="instantly", require_approval=False)),
    product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
    persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
)


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "w.db"))
    await state.init_db()
    return state


async def add(state, n):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        industry="Roofing", email=f"p{n}@co{n}.test", email_status="verified",
    ))


async def status(state, pid):
    return (await state.get_prospect(pid)).status


@pytest.mark.asyncio
async def test_a_batch_that_keeps_failing_stops_after_a_few_attempts(state):
    pid = await add(state, 1)
    brain = FakeBrain(reply="not a sequence")
    writer = Writer(brain, state, CONFIG, None)
    for _ in range(MAX_WRITE_ATTEMPTS - 1):
        await writer.run()
        assert await status(state, pid) == "new"
    await writer.run()
    assert await status(state, pid) == "write_failed"
    # The heartbeat moves on to prospecting instead of writing again.
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "prospect"
    calls = brain.calls
    await writer.run()
    assert brain.calls == calls  # no more model calls for it


@pytest.mark.asyncio
async def test_a_provider_outage_never_counts_as_an_attempt(state):
    pid = await add(state, 1)
    writer = Writer(FakeBrain(paused="Claude usage limit reached"), state, CONFIG, None)
    for _ in range(MAX_WRITE_ATTEMPTS + 2):
        await writer.run()
    assert await status(state, pid) == "new"


@pytest.mark.asyncio
async def test_success_after_a_failure_writes_normally(state):
    pid = await add(state, 1)
    brain = FakeBrain(reply="not a sequence")
    writer = Writer(brain, state, CONFIG, None)
    await writer.run()
    brain.reply = GOOD
    await writer.run()
    assert await status(state, pid) == "queued"


@pytest.mark.asyncio
async def test_attempts_are_counted_per_prospect(state):
    # Someone who joins a failing batch late gets their own full set of tries.
    early = await add(state, 1)
    writer = Writer(FakeBrain(reply="not a sequence"), state, CONFIG, None)
    for _ in range(MAX_WRITE_ATTEMPTS - 1):
        await writer.run()
    late = await add(state, 2)
    await writer.run()
    assert await status(state, early) == "write_failed"
    assert await status(state, late) == "new"


@pytest.mark.asyncio
async def test_an_empty_reply_is_an_outage_not_a_bad_draft(state):
    # On Claude a logout, rate limit, timeout or missing CLI returns "" without
    # pausing the brain. Only a reply that came back unusable may count.
    pid = await add(state, 1)
    brain = FakeBrain(reply=None)
    brain.last_reply_empty = True
    writer = Writer(brain, state, CONFIG, None)
    for _ in range(MAX_WRITE_ATTEMPTS + 2):
        await writer.run()
    assert await status(state, pid) == "new"


@pytest.mark.asyncio
async def test_the_real_brain_reports_whether_the_model_replied(state, monkeypatch):
    from sellbird.brain import Brain
    from sellbird.config import BrainConfig
    brain = Brain(state, BrainConfig())

    async def silent(*a, **kw):
        return ""

    async def chatty(*a, **kw):
        return "Sorry, here are some thoughts instead of JSON."

    monkeypatch.setattr(brain, "think", silent)
    assert await brain.think_json("x") is None and brain.last_reply_empty is True
    monkeypatch.setattr(brain, "think", chatty)
    assert await brain.think_json("x") is None and brain.last_reply_empty is False


@pytest.mark.asyncio
async def test_given_up_prospects_can_be_retried(state, capsys):
    pid = await add(state, 1)
    writer = Writer(FakeBrain(reply="not a sequence"), state, CONFIG, None)
    for _ in range(MAX_WRITE_ATTEMPTS):
        await writer.run()
    assert await status(state, pid) == "write_failed"
    assert await state.retry_write_failed() == 1
    assert await status(state, pid) == "new"
    assert await state.retry_write_failed() == 0


class _ClaudeProcess:
    """A Claude CLI that exits 0 with the given JSON result payload."""

    returncode = 0

    def __init__(self, payload):
        self.payload = payload

    async def communicate(self, data=None):
        import json
        return json.dumps(self.payload).encode(), b""


@pytest.mark.asyncio
async def test_a_cli_reported_api_error_is_no_reply_not_model_text(state, monkeypatch):
    # The CLI can report an API error (overloaded, rate limited) inside a
    # successful exit. That text is not the model's answer: no caller should
    # treat it as a draft, and the Writer must not count it as a bad one.
    import sellbird.brain as brain_mod
    from sellbird.brain import Brain

    payloads = []

    async def fake_exec(*cmd, **kwargs):
        return _ClaudeProcess(payloads.pop(0))

    async def no_sleep(*a, **kw):
        return None

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(brain_mod.asyncio, "sleep", no_sleep)
    brain = Brain(state)

    payloads[:] = [{"type": "result", "is_error": True, "result": "API Error: 529 Overloaded"}] * 5
    assert await brain.think("hi", max_retries=1) == ""
    assert await brain.think_json("hi") is None and brain.last_reply_empty is True

    payloads[:] = [{"type": "result", "is_error": False, "result": '{"ok": true}'}]
    assert await brain.think_json("hi") == {"ok": True} and brain.last_reply_empty is False
