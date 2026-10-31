"""DeepSeek brain (local fork): tool-less requests, pauses, usage and the daily cap."""

import json

import httpx
import pytest

from harvey import deepseek
from harvey.brain import Brain
from harvey.config import BrainConfig
from harvey.state import StateManager


@pytest.fixture
def state(tmp_path):
    import asyncio
    s = StateManager(db_path=str(tmp_path / "harvey.db"))
    asyncio.run(s.init_db())
    return s


def _reply(text="hello", status=200, usage=None):
    body = {"id": "req-1", "model": "deepseek-flash",
            "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20,
                               "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60}}
    return httpx.Response(status, json=body if status == 200 else {"error": {"message": "x"}})


@pytest.fixture
def api(monkeypatch):
    """Route DeepSeek's endpoint to a handler; collect what was sent."""
    sent, replies = [], []
    monkeypatch.setattr(deepseek, "api_key", lambda: "sk-test")

    def handler(request):
        sent.append(json.loads(request.content))
        return replies.pop(0) if replies else _reply()

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(deepseek.httpx, "AsyncClient", client)
    return sent, replies


@pytest.mark.asyncio
async def test_request_has_no_tools_and_records_usage(state, api):
    sent, _ = api
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("write an email", agent="writer", task="t") == "hello"
    body = sent[0]
    assert "tools" not in body and "tool_choice" not in body
    assert body["model"] == "deepseek-flash"
    assert body["messages"] == [{"role": "user", "content": "write an email"}]
    assert await state.get_usage_today(provider="deepseek") == 1
    assert await state.get_cost_today(provider="deepseek") > 0


@pytest.mark.asyncio
async def test_empty_balance_pauses_the_brain(state, api):
    _, replies = api
    replies.append(_reply(status=402))
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("x") == ""
    assert "balance" in brain.paused_reason


@pytest.mark.asyncio
async def test_tool_call_answer_is_refused(state, api):
    _, replies = api
    replies.append(httpx.Response(200, json={"choices": [{"message": {
        "content": "", "tool_calls": [{"id": "1", "type": "function",
                                       "function": {"name": "run", "arguments": "{}"}}]}}]}))
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert await brain.think("x", max_retries=0) == ""


@pytest.mark.asyncio
async def test_daily_dollar_cap_stops_harvey(state, api):
    brain = Brain(state, BrainConfig(provider="deepseek", max_daily_usd=0.00001))
    assert await brain.is_within_budget(200, max_percent=80)
    await brain.think("x")
    assert not await brain.is_within_budget(200, max_percent=80)


@pytest.mark.asyncio
async def test_preflight_needs_a_key(state, monkeypatch):
    monkeypatch.setattr(deepseek, "api_key", lambda: "")
    brain = Brain(state, BrainConfig(provider="deepseek"))
    assert "DEEPSEEK_API_KEY" in await brain.preflight()


def test_unknown_model_is_reported(monkeypatch):
    monkeypatch.setattr(deepseek, "api_key", lambda: "sk-test")
    assert "isn't a DeepSeek model" in deepseek.DeepSeekClient("gpt-5.5").configured()
