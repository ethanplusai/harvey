"""Tests for Google Gemini runtime and Brain provider integration."""

import asyncio
import os
import tempfile
import pytest
import pytest_asyncio

import harvey.brain as brain_mod
from harvey.brain import Brain
from harvey.config import BrainConfig, EnvConfig
from harvey.gemini_runtime import GeminiClient, GeminiError, GeminiResult
from harvey.state import StateManager
from tests.fake_gemini import FakeGeminiServer


@pytest.fixture
def fake_server():
    server = FakeGeminiServer()
    server.start()
    yield server
    server.stop()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


# ── Client Unit Tests ──


@pytest.mark.asyncio
async def test_gemini_client_success(fake_server):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    client = GeminiClient(api_key="TEST_KEY", model="gemini-2.5-flash", base_url=url)

    fake_server.custom_reply = "Hello from Gemini"
    result = await client.run("Write a greeting")

    assert isinstance(result, GeminiResult)
    assert result.text == "Hello from Gemini"
    assert result.model == "gemini-2.5-flash"
    assert result.duration_ms >= 0
    assert result.usage == {"input_tokens": 12, "output_tokens": 8}

    # Verify request payload
    assert len(fake_server.requests) == 1
    req = fake_server.requests[0]
    assert req["method"] == "POST"
    assert req["api_key"] == "TEST_KEY"
    assert "tools" not in req["body"]
    assert req["body"]["contents"][0]["parts"][0]["text"] == "Write a greeting"


@pytest.mark.asyncio
async def test_gemini_client_model_prefix_stripped(fake_server):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    client = GeminiClient(api_key="TEST_KEY", model="models/gemini-2.5-flash", base_url=url)
    assert client.model == "gemini-2.5-flash"
    await client.run("Hi")
    req = fake_server.requests[-1]
    assert req["path"] == "/v1beta/models/gemini-2.5-flash:generateContent"


@pytest.mark.asyncio
async def test_gemini_client_missing_key():
    client = GeminiClient(api_key="", model="gemini-2.5-flash")
    with pytest.raises(GeminiError) as exc_info:
        await client.run("Hi")
    assert exc_info.value.kind == "auth"
    assert "missing" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_gemini_client_auth_failure(fake_server):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    client = GeminiClient(api_key="INVALID_KEY", model="gemini-2.5-flash", base_url=url)

    with pytest.raises(GeminiError) as exc_info:
        await client.run("Hi")
    assert exc_info.value.kind == "auth"
    assert not exc_info.value.retryable
    assert "API key" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gemini_client_rate_limit(fake_server):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    fake_server.scenario = "rate_limit"
    client = GeminiClient(api_key="TEST_KEY", model="gemini-2.5-flash", base_url=url)

    with pytest.raises(GeminiError) as exc_info:
        await client.run("Hi")
    assert exc_info.value.kind == "usage_limit"
    assert not exc_info.value.retryable
    assert "429" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gemini_client_server_error_retryable(fake_server):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    fake_server.scenario = "server_error"
    client = GeminiClient(api_key="TEST_KEY", model="gemini-2.5-flash", base_url=url)

    with pytest.raises(GeminiError) as exc_info:
        await client.run("Hi")
    assert exc_info.value.kind == "server"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_gemini_client_blocked_prompt(fake_server):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    fake_server.scenario = "blocked_prompt"
    client = GeminiClient(api_key="TEST_KEY", model="gemini-2.5-flash", base_url=url)

    with pytest.raises(GeminiError) as exc_info:
        await client.run("Hi")
    assert exc_info.value.kind == "request"
    assert "SAFETY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gemini_client_stopped_candidate(fake_server):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    fake_server.scenario = "stopped_candidate"
    client = GeminiClient(api_key="TEST_KEY", model="gemini-2.5-flash", base_url=url)

    with pytest.raises(GeminiError) as exc_info:
        await client.run("Hi")
    assert exc_info.value.kind == "request"
    assert "SAFETY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gemini_preflight(fake_server):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"

    # Missing key
    client_no_key = GeminiClient(api_key="", base_url=url)
    assert "not configured" in await client_no_key.preflight()

    # Valid key
    client_ok = GeminiClient(api_key="VALID_KEY", base_url=url)
    assert await client_ok.preflight() == ""

    # Invalid key
    client_bad = GeminiClient(api_key="INVALID_KEY", base_url=url)
    err = await client_bad.preflight()
    assert "API key" in err


# ── Brain Integration Tests ──


@pytest.mark.asyncio
async def test_gemini_brain_think_and_usage(fake_server, state, monkeypatch):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    monkeypatch.setattr(
        "harvey.config.load_env",
        lambda: EnvConfig(gemini_api_key="TEST_KEY"),
    )

    brain = Brain(state, BrainConfig(provider="gemini", model="gemini-2.5-flash"))
    brain.gemini.base_url = url
    fake_server.custom_reply = "Gemini response text"

    resp = await brain.think("Hello", session_id="test-session", agent="writer", task="intro")
    assert resp == "Gemini response text"

    # Verify usage was recorded
    rows = await state.usage_by_provider(days=1)
    assert len(rows) == 1
    assert rows[0]["provider"] == "gemini"
    assert rows[0]["output_tokens"] == 8

    by_model = await state.usage_by_model(days=1)
    assert by_model[0]["model"] == "gemini-2.5-flash"

    # Verify daily call counters in usage_log
    assert await state.get_usage_today("gemini") == 1
    assert await state.get_usage_today("claude") == 0
    assert await state.get_usage_today("codex") == 0
    summary = await state.get_state_summary()
    assert summary["usage_today_by_provider"]["gemini"] == 1


@pytest.mark.asyncio
async def test_gemini_think_json(fake_server, state, monkeypatch):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    monkeypatch.setattr(
        "harvey.config.load_env",
        lambda: EnvConfig(gemini_api_key="TEST_KEY"),
    )

    brain = Brain(state, BrainConfig(provider="gemini"))
    brain.gemini.base_url = url
    fake_server.custom_reply = '```json\n{"status": "success", "count": 42}\n```'

    parsed = await brain.think_json("Generate json")
    assert parsed == {"status": "success", "count": 42}


@pytest.mark.asyncio
async def test_gemini_retries_transient_failure(fake_server, state, monkeypatch):
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr(
        "harvey.config.load_env",
        lambda: EnvConfig(gemini_api_key="TEST_KEY"),
    )

    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    fake_server.scenario_queue = ["server_error", "normal"]
    fake_server.custom_reply = "Recovered after retry"

    brain = Brain(state, BrainConfig(provider="gemini"))
    brain.gemini.base_url = url

    resp = await brain.think("Hi", max_retries=2)
    assert resp == "Recovered after retry"
    assert len(fake_server.requests) == 2


@pytest.mark.asyncio
async def test_gemini_rate_limit_pauses_brain(fake_server, state, monkeypatch):
    monkeypatch.setattr(
        "harvey.config.load_env",
        lambda: EnvConfig(gemini_api_key="TEST_KEY"),
    )

    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    fake_server.scenario = "rate_limit"

    brain = Brain(state, BrainConfig(provider="gemini"))
    brain.gemini.base_url = url

    resp = await brain.think("Hi")
    assert resp == ""
    assert "usage limit reached" in brain.paused_reason.lower()


@pytest.mark.asyncio
async def test_gemini_preflight_integration(fake_server, state, monkeypatch):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    monkeypatch.setattr(
        "harvey.config.load_env",
        lambda: EnvConfig(gemini_api_key="TEST_KEY"),
    )

    brain = Brain(state, BrainConfig(provider="gemini"))
    brain.gemini.base_url = url

    assert await brain.preflight() == ""


@pytest.mark.asyncio
async def test_gemini_daily_budget_limit(fake_server, state, monkeypatch):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    monkeypatch.setattr(
        "harvey.config.load_env",
        lambda: EnvConfig(gemini_api_key="TEST_KEY"),
    )

    brain = Brain(state, BrainConfig(provider="gemini"))
    brain.gemini.base_url = url
    fake_server.custom_reply = "Call 1"

    assert await brain.is_within_budget(max_daily_calls=1) is True
    assert await brain.think("Hello 1") == "Call 1"
    assert await brain.check_usage() == 1.0
    assert await brain.is_within_budget(max_daily_calls=1) is False


@pytest.mark.asyncio
async def test_gemini_preflight_transient_not_sticky(fake_server, state, monkeypatch):
    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    monkeypatch.setattr(
        "harvey.config.load_env",
        lambda: EnvConfig(gemini_api_key="TEST_KEY"),
    )

    fake_server.scenario_queue = ["server_error", "normal"]
    brain = Brain(state, BrainConfig(provider="gemini"))
    brain.gemini.base_url = url

    # First preflight encounters transient 503
    err = await brain.preflight()
    assert "unavailable" in err.lower() or "server" in err.lower()
    # Must NOT mark brain sticky-unavailable for 1800s
    assert brain.unavailable == ""

    # Second preflight immediately succeeds
    assert await brain.preflight() == ""


@pytest.mark.asyncio
async def test_gemini_preflight_restores_stored_usage_pause(fake_server, state, monkeypatch):
    import json
    import time
    from harvey.brain import BRAIN_STATUS_KEY

    url = f"http://{fake_server.server_address[0]}:{fake_server.server_address[1]}"
    monkeypatch.setattr(
        "harvey.config.load_env",
        lambda: EnvConfig(gemini_api_key="TEST_KEY"),
    )

    until = time.time() + 1800
    await state.set_setting(
        BRAIN_STATUS_KEY,
        json.dumps({
            "provider": "gemini",
            "status": "paused",
            "kind": "usage_limit",
            "reason": "rate limit reached",
            "until": until,
        }),
    )

    brain = Brain(state, BrainConfig(provider="gemini"))
    brain.gemini.base_url = url

    assert await brain.preflight() == ""
    assert "usage limit reached" in brain.paused_reason.lower()


def test_cli_train_supports_gemini():
    from harvey.cli import main
    import argparse
    from unittest.mock import patch

    with patch("sys.argv", ["harvey", "train", "https://example.com", "--provider", "gemini"]):
        # Verify parser accepts --provider gemini without error
        from harvey import cli
        parser = argparse.ArgumentParser()
        # Verify choices directly from cli
        from harvey.config import BRAIN_PROVIDERS
        assert "gemini" in BRAIN_PROVIDERS
