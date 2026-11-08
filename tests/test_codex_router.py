import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from sellbird.brain import Brain
from sellbird.codex import CodexError
from sellbird.codex_router import RouterClient, RouterQuotaClient, validate_router_url
from sellbird.config import BrainConfig
from sellbird.state import StateManager


def reply(**changes):
    data = {
        "id": "resp_test", "status": "completed", "model": "gpt-6.1-sol",
        "output": [{"type": "reasoning"}, {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": '{"ok": true}'}]}],
        "usage": {"input_tokens": 20, "output_tokens": 5,
                  "input_tokens_details": {"cached_tokens": 3}},
    }
    return data | changes


def client(handler):
    return RouterClient("ar/gpt-6.1-sol", "high",
                        transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("url", [
    "https://example.com:443/v1", "http://localhost:20129/v1",
    "http://127.0.0.1/v1", "http://127.0.0.1:20129/v1?key=secret",
    "http://user:secret@127.0.0.1:20129/v1", "file:///v1",
    "http://127.0.0.1:20129/other",
])
def test_url_refusal(url):
    with pytest.raises(ValueError):
        validate_router_url(url)


def test_config_opt_in_and_existing_default():
    assert BrainConfig().provider == "claude"
    assert BrainConfig(provider="codex", model="gpt-5.5").provider == "codex"
    assert BrainConfig(provider="codex_router", model="ar/gpt-6.1-sol").model == "ar/gpt-6.1-sol"
    with pytest.raises(ValidationError):
        BrainConfig(provider="codex_router")
    with pytest.raises(ValidationError):
        BrainConfig(provider="codex_router", model="ar/gpt-6.1-sol",
                    router_base_url="https://example.com:443/v1")


@pytest.mark.asyncio
async def test_payload_and_usage():
    def handler(request):
        payload = json.loads(request.content)
        assert request.url == "http://127.0.0.1:20129/v1/responses"
        assert "authorization" not in request.headers
        assert payload["model"] == "ar/gpt-6.1-sol"
        assert payload["tools"] == []
        assert payload["tool_choice"] == "none"
        assert payload["stream"] is False and payload["store"] is False
        assert payload["reasoning"] == {"effort": "high"}
        assert payload["input"][0]["content"][0]["text"] == "test prompt"
        return httpx.Response(200, json=reply())
    result = await client(handler).run("test prompt", timeout=1)
    assert result.text == '{"ok": true}'
    assert result.model == "ar/gpt-6.1-sol"
    assert result.usage["input_tokens"] == 17
    assert result.usage["cache_read_tokens"] == 3


@pytest.mark.parametrize("status,kind,retryable", [
    (401, "auth", False), (403, "auth", False), (429, "usage_limit", False),
    (400, "setup", False), (404, "setup", False),
    (502, "transport", False), (302, "transport", False),
])
@pytest.mark.asyncio
async def test_http_errors_redacted(status, kind, retryable):
    c = client(lambda _: httpx.Response(status, text="secret credential body"))
    with pytest.raises(CodexError) as error:
        await c.run("test", timeout=1)
    assert error.value.kind == kind and error.value.retryable == retryable
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("data", [
    reply(status="incomplete"),
    reply(output=[]),
    reply(output=[{"type": "function_call", "name": "shell"}]),
    reply(output=[{"type": "message", "role": "assistant", "content": [
        {"type": "refusal", "refusal": "no"}]}]),
    reply(output=[None]),
    reply(usage={"input_tokens": "not-a-number"}),
    reply(usage={"input_tokens": float("inf")}),
    reply(model="gpt-5.5"),
    reply(model=None),
])
@pytest.mark.asyncio
async def test_invalid_and_tool_output_fails(data):
    with pytest.raises(CodexError):
        await client(lambda _: httpx.Response(
            200, content=json.dumps(data))).run("test", timeout=1)


@pytest.mark.parametrize("model", ["ar/gpt-6.1-sol", "gpt-6.1-sol"])
@pytest.mark.asyncio
async def test_exact_or_unprefixed_response_model(model):
    result = await client(lambda _: httpx.Response(200, json=reply(model=model))).run(
        "test", timeout=1)
    assert result.model == "ar/gpt-6.1-sol"


@pytest.mark.asyncio
async def test_timeout_not_retried_and_pause_not_sent():
    async def handler(_):
        await asyncio.sleep(1)
        return httpx.Response(200, json=reply())
    with pytest.raises(CodexError) as error:
        await client(handler).run("test", timeout=.01)
    assert error.value.kind == "timeout" and not error.value.retryable
    def forbidden(_):
        raise AssertionError("must not send")
    with pytest.raises(CodexError) as error:
        await client(forbidden).run("test", timeout=1, skip_if=lambda: "paused")
    assert error.value.kind == "paused"
    assert await RouterQuotaClient().get_utilization() is None


@pytest.mark.asyncio
async def test_brain_usage_and_no_fallback(tmp_path):
    state = StateManager(str(tmp_path / "test.db"))
    await state.init_db()
    brain = Brain(state, BrainConfig(provider="codex_router", model="ar/gpt-6.1-sol"))
    brain.codex = client(lambda _: httpx.Response(200, json=reply()))
    assert await brain.think("test", max_retries=0) == '{"ok": true}'
    assert await state.get_usage_today(provider="codex_router") == 1
    summary = await state.get_state_summary()
    assert summary["usage_today_by_provider"]["codex_router"] == 1
    assert await state.get_usage_today(provider="codex") == 0
    assert await state.get_usage_today(provider="claude") == 0
    assert not await brain.is_within_budget(max_daily_calls=1, max_percent=90)
    brain.codex = client(lambda _: httpx.Response(429, text="secret"))
    assert await brain.think("test", max_retries=0) == ""
    assert not await brain.is_within_budget(max_daily_calls=10)
    assert brain.provider == "codex_router"
    assert await state.get_usage_today(provider="codex_router") == 2


@pytest.mark.asyncio
async def test_accounting_failure_blocks_paid_request(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "test.db"))
    await state.init_db()
    brain = Brain(state, BrainConfig(provider="codex_router", model="ar/gpt-6.1-sol"))
    async def broken(*args, **kwargs):
        raise RuntimeError("database offline")
    monkeypatch.setattr(state, "increment_usage", broken)
    monkeypatch.setattr(state, "get_usage_today", broken)
    brain.codex = client(lambda _: pytest.fail("must not send a paid request"))
    assert await brain.think("test", max_retries=0) == ""
    assert not await brain.is_within_budget(max_daily_calls=200)
