"""LiteLLM calls use the gateway, keep usage separate and never expose errors."""

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.brain as brain_mod
import harvey.dashboard as dash
import harvey.litellm_gateway as gateway
from harvey.brain import Brain
from harvey.config import BrainConfig
from harvey.state import StateManager
from harvey.trainer import brain_section
from tests.test_dashboard_signals import client as dashboard_client  # noqa: F401
from tests.test_brain_provider import _render_js


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example.com/v1/")
    monkeypatch.setenv("LITELLM_API_KEY", "test-virtual-key")
    monkeypatch.setattr(gateway, "load_dotenv", lambda *a, **k: None)
    original = httpx.AsyncClient
    requests = []
    reply = {
        "status": 200,
        "body": {
            "id": "completion-test",
            "model": "sales-model",
            "choices": [
                {
                    "message": {
                        "content": '{"subject":"Hello","body":"A fictional draft."}'
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 12},
        },
    }

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "sales-model"}]})
        return httpx.Response(reply["status"], json=reply["body"])

    monkeypatch.setattr(
        gateway.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(handle)),
    )
    return requests, reply


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "test.db"))
    await state.init_db()
    return state


def test_should_require_an_explicit_gateway_model():
    assert BrainConfig().provider == "claude"
    with pytest.raises(ValidationError, match="model alias"):
        BrainConfig(provider="litellm")
    config = BrainConfig(provider=" LiteLLM ", model="sales-model")
    assert brain_section(config) == {"provider": "litellm", "model": "sales-model"}


@pytest.mark.asyncio
async def test_should_route_json_without_cli_tools_and_record_usage(
    transport, state, monkeypatch
):
    def no_cli(*a, **kw):
        pytest.fail("LiteLLM must not start a CLI")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_cli)
    brain = Brain(state, BrainConfig(provider="litellm", model="sales-model"))
    assert await brain.preflight() == ""
    result = await brain.think_json(
        "Write a fictional draft", agent="writer", task="draft"
    )
    assert result["subject"] == "Hello"
    requests, _ = transport
    assert [r.url.path for r in requests] == ["/v1/models", "/v1/chat/completions"]
    assert requests[-1].headers["Authorization"] == "Bearer test-virtual-key"
    payload = json.loads(requests[-1].content)
    assert payload["model"] == "sales-model" and not payload["stream"]
    assert set(payload) == {"model", "messages", "stream"}
    assert await state.get_usage_today(provider="litellm") == 1
    assert await state.get_usage_today(provider="claude") == 0
    assert await state.get_usage_today(provider="codex") == 0
    rows = await state.usage_by_provider()
    assert rows[0]["provider"] == "litellm"
    assert rows[0]["input_tokens"] == 42 and rows[0]["output_tokens"] == 12
    assert await brain.is_within_budget(max_daily_calls=2, max_percent=80)
    assert not await brain.is_within_budget(max_daily_calls=1, max_percent=80)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,retries", [(401, 1), (403, 1), (400, 1), (429, 3), (503, 3)]
)
async def test_should_retry_only_transient_failures_without_exposing_details(
    transport, state, monkeypatch, caplog, status, retries
):
    requests, reply = transport
    reply.update(status=status, body={"error": "private-response-canary"})
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)
    brain = Brain(state, BrainConfig(provider="litellm", model="sales-model"))
    assert await brain.think("private-prompt-canary") == ""
    assert len(requests) == retries
    assert "private-response-canary" not in caplog.text
    assert "private-prompt-canary" not in caplog.text
    assert "test-virtual-key" not in caplog.text
    assert await state.get_usage_today(provider="litellm") == 0


@pytest.mark.asyncio
async def test_should_recover_after_fixing_gateway_access(transport, state):
    _, reply = transport
    brain = Brain(state, BrainConfig(provider="litellm", model="sales-model"))
    reply["status"] = 401
    assert await brain.think("test") == ""
    assert brain.paused_reason
    reply["status"] = 200
    assert await brain.preflight() == ""
    assert not brain.paused_reason
    assert await brain.think("test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://gateway.example.com",
        "https://user:pass@gateway.example.com",
        "https://gateway.example.com?key=private",
        "https://gateway.example.com#private",
        "file:///tmp/gateway",
    ],
)
async def test_should_reject_unsafe_urls_before_sending_a_key(
    transport, monkeypatch, url
):
    monkeypatch.setenv("LITELLM_BASE_URL", url)
    with pytest.raises(gateway.LiteLLMError, match="LITELLM_BASE_URL"):
        await gateway.LiteLLMClient("sales-model").run("test", timeout=1)
    assert transport[0] == []


@pytest.mark.asyncio
async def test_should_require_the_virtual_key(transport, monkeypatch):
    monkeypatch.delenv("LITELLM_API_KEY")
    with pytest.raises(gateway.LiteLLMError, match="LITELLM_API_KEY"):
        await gateway.LiteLLMClient("sales-model").preflight()
    assert transport[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {
            "id": "x",
            "model": "sales-model",
            "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
        },
    ],
)
async def test_should_leave_failed_completions_uncounted(transport, state, body):
    transport[1]["body"] = body
    brain = Brain(state, BrainConfig(provider="litellm", model="sales-model"))
    assert await brain.think("test") == ""
    assert await state.get_usage_today(provider="litellm") == 0


def test_should_display_gateway_calls_and_usage_without_claude_quota(
    dashboard_client, monkeypatch  # noqa: F811 (imported pytest fixture)
):
    state = StateManager(str(dashboard_client.db_path))
    asyncio.run(state.init_db())
    asyncio.run(state.increment_usage(provider="litellm"))
    asyncio.run(
        state.record_usage_event(
            provider="litellm", model="sales-model", output_tokens=12
        )
    )
    monkeypatch.setattr(
        dash,
        "_active_brain",
        lambda: BrainConfig(provider="litellm", model="sales-model"),
    )
    stats = dashboard_client.get("/api/stats").json()
    assert stats["litellm_calls_today"] == 1
    chips = _render_js("brainCallChips", stats)
    assert "LiteLLM AI Gateway" in chips and "Claude" not in chips
    data = dashboard_client.get("/api/usage").json()
    assert (
        data["provider"] == "litellm"
        and data["by_provider"][0]["provider"] == "litellm"
    )
    card = _render_js("quotaCardHtml", data)
    assert "LiteLLM AI Gateway" in card and "Claude" not in card


def test_should_keep_the_gateway_choice_in_setup(monkeypatch):
    import harvey.setup as setup

    answers = iter(["litellm", "sales-model"])
    monkeypatch.setattr(setup, "_ask", lambda *a, **kw: next(answers))
    assert setup._ask_brain_config() == BrainConfig(
        provider="litellm", model="sales-model"
    )


@pytest.mark.asyncio
async def test_should_clear_a_stale_gateway_pause_after_restart(transport, state):
    await state.set_setting(
        brain_mod.BRAIN_STATUS_KEY,
        json.dumps(
            {"provider": "litellm", "status": "paused", "reason": "old failure"}
        ),
    )
    brain = Brain(state, BrainConfig(provider="litellm", model="sales-model"))
    assert await brain.preflight() == ""
    status = json.loads(await state.get_setting(brain_mod.BRAIN_STATUS_KEY))
    assert status["status"] == "ok"


@pytest.mark.asyncio
async def test_should_preserve_a_completion_when_accounting_fails(
    transport, state, monkeypatch
):
    async def fail(**kw):
        raise RuntimeError("private-accounting-canary")

    monkeypatch.setattr(state, "increment_usage", fail)
    brain = Brain(state, BrainConfig(provider="litellm", model="sales-model"))
    assert await brain.think("test")
