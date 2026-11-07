"""DeepSeek brain: tool-less requests, pauses, usage, the dollar cap, and that
Claude and Codex are untouched. No real request is ever made: httpx is routed
to a fake endpoint."""

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from sellbird import brain as brain_mod
from sellbird import deepseek
from sellbird.brain import Brain
from sellbird.config import BrainConfig
from sellbird.state import MIGRATIONS, StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "sellbird.db"))
    await s.init_db()
    return s


def _reply(text="hello", status=200, usage=None):
    if status != 200:
        return httpx.Response(status, json={"error": {"message": "fake"}})
    return httpx.Response(200, json={
        "id": "req-1", "model": "deepseek-flash",
        "choices": [{"message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20,
                           "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60},
    })


@pytest.fixture
def api(monkeypatch):
    """Route DeepSeek's endpoint to a queue of replies; collect what was sent."""
    sent, replies = [], []
    key = {"value": "sk-test"}
    monkeypatch.setattr(deepseek, "api_key", lambda: key["value"])
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)

    def handler(request):
        sent.append({"body": json.loads(request.content),
                     "auth": request.headers.get("authorization")})
        return replies.pop(0) if replies else _reply()

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(deepseek.httpx, "AsyncClient", client)
    return sent, replies, key


# ── the request ──


@pytest.mark.asyncio
async def test_request_is_text_only_and_records_usage(state, api):
    sent, _, _ = api
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("write an email", agent="writer", task="t") == "hello"
    body = sent[0]["body"]
    assert "tools" not in body and "tool_choice" not in body
    assert body["model"] == "deepseek-flash"
    assert body["messages"] == [{"role": "user", "content": "write an email"}]
    # Thinking is DeepSeek's default and bills reasoning tokens: always off.
    assert body["thinking"] == {"type": "disabled"}
    assert sent[0]["auth"] == "Bearer sk-test"
    assert await state.get_usage_today(provider="deepseek") == 1
    assert await state.get_usage_today(provider="claude") == 0
    assert await state.get_cost_today(provider="deepseek") > 0
    rows = await state.usage_by_provider(days=1)
    assert [(r["provider"], r["output_tokens"], r["cache_read_tokens"]) for r in rows] == [
        ("deepseek", 20, 40)]


@pytest.mark.asyncio
async def test_think_json(state, api):
    _, replies, _ = api
    replies.append(_reply('```json\n{"score": 7}\n```'))
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think_json("score this") == {"score": 7}


def test_cost_is_estimated_at_peak_rates():
    usage = {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000,
             "completion_tokens": 1_000_000}
    assert deepseek.estimate_cost("deepseek-flash", usage) == pytest.approx(0.006 + 0.30 + 1.20)
    # An unknown model is priced as the dearest, so the cap never under-counts.
    assert deepseek.estimate_cost("something-new", usage) == pytest.approx(0.044 + 1.32 + 3.96)


# ── failures ──


@pytest.mark.asyncio
async def test_rate_limit_and_server_errors_are_retried(state, api):
    sent, replies, _ = api
    replies.extend([_reply(status=429), _reply(status=503), _reply("third time")])
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("x") == "third time"
    assert len(sent) == 3
    assert brain.paused_reason == ""


@pytest.mark.asyncio
async def test_retries_are_bounded(state, api):
    sent, replies, _ = api
    replies.extend([_reply(status=500)] * 5)
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("x", max_retries=2) == ""
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_bad_request_is_not_retried(state, api):
    sent, replies, _ = api
    replies.append(_reply(status=422))
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("x") == ""
    assert len(sent) == 1 and brain.paused_reason == ""


@pytest.mark.asyncio
async def test_tool_call_answer_is_refused(state, api):
    _, replies, _ = api
    replies.append(httpx.Response(200, json={"choices": [{"message": {
        "content": "", "tool_calls": [{"id": "1", "type": "function",
                                       "function": {"name": "run", "arguments": "{}"}}]}}]}))
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("x") == ""
    assert await state.get_usage_today(provider="deepseek") == 0


@pytest.mark.asyncio
async def test_empty_balance_pauses_until_the_next_check(state, api):
    sent, replies, _ = api
    replies.append(_reply(status=402))
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("x") == ""
    assert "balance" in brain.paused_reason
    # While paused, calls aren't sent at all.
    assert await brain.think("y") == ""
    assert len(sent) == 1
    status = json.loads(await state.get_setting(brain_mod.BRAIN_STATUS_KEY))
    assert status["provider"] == "deepseek" and status["status"] == "paused"


@pytest.mark.asyncio
async def test_rejected_key_stays_paused_until_the_key_changes(state, api):
    sent, replies, key = api
    replies.append(_reply(status=401))
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("x") == ""
    # Same key: the next heartbeat's preflight keeps the pause, no request.
    assert "401" in await brain.preflight()
    assert len(sent) == 1
    # A new key in .env resumes without a restart.
    key["value"] = "sk-new"
    assert await brain.preflight() == ""
    assert await brain.think("x") == "hello"
    assert sent[-1]["auth"] == "Bearer sk-new"


@pytest.mark.asyncio
async def test_preflight_needs_a_key_and_sends_nothing(state, api):
    sent, _, key = api
    key["value"] = ""
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert "DEEPSEEK_API_KEY" in await brain.preflight()
    assert sent == []


def test_unknown_model_is_reported(monkeypatch):
    monkeypatch.setattr(deepseek, "api_key", lambda: "sk-test")
    assert "isn't a DeepSeek model" in deepseek.DeepSeekClient("gpt-5.5").configured()
    assert deepseek.DeepSeekClient("").configured() == ""
    assert deepseek.DeepSeekClient("deepseek-v4-pro").configured() == ""


def test_api_key_comes_from_the_env_file_first(tmp_path, monkeypatch):
    import sellbird.paths as paths

    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-environment")
    assert deepseek.api_key() == "sk-from-environment"
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=sk-from-file\n", encoding="utf-8")
    assert deepseek.api_key() == "sk-from-file"


# ── budget ──


@pytest.mark.asyncio
async def test_daily_dollar_cap_stops_sellbird(state, api):
    brain = Brain(state, BrainConfig(provider="deepseek", max_daily_usd=0.00001))
    assert await brain.is_within_budget(200, max_percent=80)
    await brain.think("x")
    assert not await brain.is_within_budget(200, max_percent=80)


@pytest.mark.asyncio
async def test_zero_cap_leaves_only_the_call_limit(state, api):
    brain = Brain(state, BrainConfig(provider="deepseek", max_daily_usd=0))
    await brain.think("x")
    assert await brain.is_within_budget(200, max_percent=80)
    assert not await brain.is_within_budget(1, max_percent=80)


@pytest.mark.asyncio
async def test_deepseek_spend_never_touches_the_claude_budget(state, api):
    deep = Brain(state, BrainConfig(provider="deepseek", max_daily_usd=0.00001))
    await deep.think("x")
    claude = Brain(state, BrainConfig())

    class Quota:
        async def get_utilization(self):
            return None

    claude.quota = Quota()
    assert await claude.is_within_budget(1, max_percent=80)


# ── config and migration ──


def test_brain_config():
    assert BrainConfig().max_daily_usd == 0.50
    assert BrainConfig(provider="DeepSeek").provider == "deepseek"
    assert BrainConfig(provider="deepseek", max_daily_usd="2").max_daily_usd == 2.0
    for bad in (-1, "lots"):
        with pytest.raises(ValidationError):
            BrainConfig(provider="deepseek", max_daily_usd=bad)


def test_retraining_keeps_the_dollar_cap():
    from sellbird.trainer import brain_section

    assert brain_section(BrainConfig(provider="deepseek", max_daily_usd=1.25)) == {
        "provider": "deepseek", "max_daily_usd": 1.25}
    # Claude and Codex configs are written back exactly as before.
    assert brain_section(BrainConfig()) == {"provider": "claude"}


@pytest.mark.asyncio
async def test_older_database_gets_a_deepseek_counter(tmp_path):
    from datetime import date

    import aiosqlite

    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        for script in MIGRATIONS[:14]:  # a database from before v15
            await db.executescript(script)
        await db.execute("PRAGMA user_version = 14")
        await db.execute(
            "INSERT INTO usage_log (id, date, claude_calls, codex_calls) VALUES ('u', ?, 5, 3)",
            (date.today().isoformat(),))
        await db.commit()

    sm = StateManager(db_path)
    await sm.init_db()
    assert await sm.get_usage_today("claude") == 5
    assert await sm.get_usage_today("codex") == 3
    assert await sm.get_usage_today("deepseek") == 0
    await sm.increment_usage(provider="deepseek")
    assert await sm.get_usage_today("deepseek") == 1
    assert await sm.get_usage_today("claude") == 5


# ── dashboard ──


def test_dashboard_shows_spend_instead_of_a_quota(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import sellbird.dashboard as dash

    db = tmp_path / "sellbird.db"
    sm = StateManager(str(db))
    asyncio.run(sm.init_db())
    asyncio.run(sm.record_usage_event(provider="deepseek", model="deepseek-flash",
                                      output_tokens=9, cost_usd=0.02))
    asyncio.run(sm.increment_usage(provider="deepseek"))
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_quota_clients", {})
    monkeypatch.setattr(dash, "_active_brain",
                        lambda: BrainConfig(provider="deepseek", max_daily_usd=0.10))
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as client:
        usage = client.get("/api/usage").json()
        stats = client.get("/api/stats").json()
    assert usage["provider"] == "deepseek" and usage["quota"] is None
    assert usage["spend"] == {"today_usd": pytest.approx(0.02), "cap_usd": 0.10}
    assert stats["brain_provider"] == "deepseek"
    assert stats["deepseek_calls_today"] == 1 and stats["claude_calls_today"] == 0


def test_dashboard_renders_spend_and_calls():
    from tests.test_brain_provider import _render_js

    card = _render_js("quotaCardHtml", {
        "quota_provider": "deepseek", "quota": None,
        "spend": {"today_usd": 0.05, "cap_usd": 0.10}},
        helpers=("escHtml", "formatDate", "spendCardHtml"))
    assert "DeepSeek Spend Today" in card and "$0.050 of $0.10" in card and "50%" in card
    assert "Claude" not in card

    chips = _render_js("brainCallChips", {
        "brain_provider": "deepseek", "deepseek_calls_today": 4, "claude_calls_today": 0,
        "codex_calls_today": 0, "brain_status": {}}, helpers=("escHtml",))
    assert "DeepSeek calls today <b>4</b>" in chips and "Claude" not in chips
