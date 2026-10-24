"""OpenCode Go: direct inference calls, usage accounting and allowance quota.

Every test is offline: ``httpx.MockTransport`` stands in for the Go API, and
Brain tests use a stub adapter. No key, no network, no Go spend.
"""

import os
import tempfile
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.brain as brain_mod
import harvey.opencode as oc
from harvey.brain import Brain
from harvey.config import BrainConfig
from harvey.opencode import (
    OpenCodeClient,
    OpenCodeError,
    OpenCodeQuotaClient,
    OpenCodeResult,
)
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def _opencode_usage(input_tokens=8, output_tokens=5, cache_read=2,
                    cache_write_5m=0, cache_write_1h=0, allowance=0.00001):
    return {
        "billing_source": "go",
        "list_cost": allowance * 1.3,
        "allowance": allowance,
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "reasoning": 0,
            "cache_read": cache_read,
            "cache_write_5m": cache_write_5m,
            "cache_write_1h": cache_write_1h,
        },
    }


def _chat_body(text="hello", **usage):
    return {
        "id": "chatcmpl-test",
        "model": "deepseek-v4.1-flash",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 2},
            "cost": 0,
            "opencode": _opencode_usage(**usage),
        },
    }


def _client(handler, **kwargs):
    return OpenCodeClient(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


# ── request shape ──


@pytest.mark.asyncio
async def test_chat_call_sends_required_headers_and_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_chat_body("OK"))

    result = await _client(handler).run("Say OK", session_id="harvey-writer")

    assert seen["url"] == (
        "https://opencode.ai/inference/go/openai/v1/chat/completions")
    assert seen["headers"]["authorization"] == "Bearer test-key"
    assert seen["headers"]["x-opencode-session"] == "harvey-writer"
    assert seen["headers"]["user-agent"] == oc.USER_AGENT
    assert seen["body"]["model"] == "deepseek-v4.1-flash"
    assert seen["body"]["messages"] == [{"role": "user", "content": "Say OK"}]
    assert seen["body"]["max_tokens"] == 8192
    assert seen["body"]["stream"] is False

    assert result.text == "OK"
    assert result.usage["input_tokens"] == 8
    assert result.usage["output_tokens"] == 5
    assert result.usage["cache_read_tokens"] == 2
    assert result.cost_usd == pytest.approx(0.00001)
    assert result.billing_source == "go"


@pytest.mark.asyncio
async def test_session_id_is_header_safe():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["session"] = request.headers["x-opencode-session"]
        return httpx.Response(200, json=_chat_body())

    await _client(handler).run("hi", session_id="harvey writer\n/../../etc")
    assert seen["session"].startswith("harvey-writer")
    assert " " not in seen["session"]
    assert "/" not in seen["session"]
    assert "\n" not in seen["session"]


# ── response styles ──


@pytest.mark.asyncio
async def test_anthropic_style_request_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "msg-test",
            "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "OK"}],
            "usage": {
                "input_tokens": 8,
                "output_tokens": 3,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
                "opencode": _opencode_usage(
                    input_tokens=8, output_tokens=3, cache_read=2,
                    cache_write_5m=1, allowance=0.00002),
            },
        })

    result = await _client(handler, model="claude-haiku-5-5").run("Say OK")

    assert seen["url"] == (
        "https://opencode.ai/inference/go/anthropic/v1/messages")
    assert seen["body"]["max_tokens"] == 8192
    assert seen["body"]["messages"] == [{"role": "user", "content": "Say OK"}]
    assert result.text == "OK"
    assert result.usage["cache_creation_tokens"] == 1
    assert result.cost_usd == pytest.approx(0.00002)


@pytest.mark.asyncio
async def test_responses_style_request_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "resp-test",
            "model": "gpt-6-luna",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "OK"}],
            }],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 2},
                "opencode": _opencode_usage(),
            },
        })

    result = await _client(handler, model="gpt-6-luna").run("Say OK")

    assert seen["url"] == "https://opencode.ai/inference/go/openai/v1/responses"
    assert seen["body"]["input"] == "Say OK"
    assert seen["body"]["max_output_tokens"] == 8192
    assert result.text == "OK"


@pytest.mark.asyncio
async def test_google_style_request_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "cachedContentTokenCount": 2,
            },
        })

    result = await _client(handler, model="gemini-4-argon-contributor").run("Say OK")

    assert seen["url"] == (
        "https://opencode.ai/inference/go/google/v1beta/models/"
        "gemini-4-argon-contributor:generateContent")
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "Say OK"
    assert seen["body"]["generationConfig"]["maxOutputTokens"] == 8192
    assert result.text == "OK"


# ── errors ──


@pytest.mark.asyncio
async def test_auth_error_is_not_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error": {"type": "server_error",
                      "message": "Upstream request failed: Go requires a credential"}})

    with pytest.raises(OpenCodeError) as caught:
        await _client(handler).run("hi")
    assert caught.value.kind == "auth"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_usage_limit_carries_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "120"}, json={
            "error": {"type": "usage_limit", "message": "Go allowance exhausted"}})

    with pytest.raises(OpenCodeError) as caught:
        await _client(handler).run("hi")
    assert caught.value.kind == "usage_limit"
    assert caught.value.retry_after == 120.0


@pytest.mark.asyncio
async def test_missing_session_header_is_an_isolation_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"type": "MissingSessionID",
                      "message": "Request is missing x-opencode-session"}})

    with pytest.raises(OpenCodeError) as caught:
        await _client(handler).run("hi")
    assert caught.value.kind == "isolation"


@pytest.mark.asyncio
async def test_timeout_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(OpenCodeError) as caught:
        await _client(handler).run("hi")
    assert caught.value.kind == "timeout"
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_empty_reply_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "x", "model": "deepseek-v4.1-flash",
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        })

    with pytest.raises(OpenCodeError) as caught:
        await _client(handler).run("hi")
    assert caught.value.retryable is True


# ── preflight / catalog ──


@pytest.mark.asyncio
async def test_preflight_checks_the_model_catalog():
    oc.clear_catalog_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/v1/models")
        return httpx.Response(200, json={"data": [
            {"id": "deepseek-v4.1-flash"}, {"id": "glm-5.3-flash"}]})

    client = _client(handler)
    await client.preflight()  # no exception

    bad = _client(handler, model="deepseek-v4.2")
    with pytest.raises(OpenCodeError) as caught:
        await bad.preflight()
    assert caught.value.kind == "setup"
    assert "deepseek-v4.1-flash" in str(caught.value)
    oc.clear_catalog_cache()


@pytest.mark.asyncio
async def test_missing_key_is_a_setup_error(monkeypatch):
    monkeypatch.setattr(oc, "api_key_from_env", lambda: "")
    client = OpenCodeClient(model="deepseek-v4.1-flash")
    with pytest.raises(OpenCodeError) as caught:
        await client.run("hi")
    assert caught.value.kind == "setup"
    assert "OPENCODE_API_KEY" in str(caught.value)


# ── Brain integration ──


class _StubOpenCode:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.model = "deepseek-v4.1-flash"
        self.preflight_outcomes = []
        self.preflights = 0
        self.print = ("key-v1",)

    def fingerprint(self):
        return self.print

    async def preflight(self):
        self.preflights += 1
        if self.preflight_outcomes:
            outcome = self.preflight_outcomes.pop(0)
            if outcome is not None:
                raise outcome

    async def run(self, prompt, session_id="", timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _opencode_brain(state, outcomes):
    brain = Brain(state, BrainConfig(provider="opencode"))
    brain.opencode = _StubOpenCode(outcomes)
    return brain


@pytest.mark.asyncio
async def test_brain_uses_opencode_and_records_usage(state, monkeypatch):
    async def no_claude(*a, **k):
        raise AssertionError("OpenCode brain must not run the Claude CLI")

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", no_claude)
    result = OpenCodeResult(
        text="hello", model="deepseek-v4.1-flash", request_id="req-1",
        duration_ms=900, cost_usd=0.00002,
        usage={"input_tokens": 10, "output_tokens": 4,
               "cache_read_tokens": 2, "cache_creation_tokens": 0},
        billing_source="go",
    )
    brain = _opencode_brain(state, [result])
    assert type(brain.quota).__name__ == "OpenCodeQuotaClient"
    assert await brain.think("hi", session_id="harvey-writer",
                             agent="writer", task="draft") == "hello"

    assert await state.get_usage_today("opencode") == 1
    assert await state.get_usage_today("claude") == 0
    rows = await state.usage_by_provider(days=1)
    assert rows == [{**rows[0], "provider": "opencode", "calls": 1,
                     "output_tokens": 4, "cost_usd": 0.00002}]
    by_model = await state.usage_by_model(days=1)
    assert by_model[0]["model"] == "deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_brain_auth_failure_pauses_and_does_not_call_again(state):
    brain = _opencode_brain(state, [OpenCodeError("bad key", kind="auth")])
    assert await brain.think("hi") == ""
    assert "bad key" in brain.unavailable
    # A paused provider is skipped without another HTTP call.
    assert await brain.think("again") == ""
    assert brain.opencode.calls == 1


@pytest.mark.asyncio
async def test_opencode_auth_pause_sticks_until_the_key_changes(state):
    import time

    brain = _opencode_brain(state, [])
    brain.opencode.preflight_outcomes = [None, None]
    brain.unavailable = "bad key"
    brain._unavailable_at = time.monotonic()
    brain._unavailable_fingerprint = brain._fingerprint()

    # The catalog endpoint is public, so a successful preflight must not
    # clear a rejected-key pause by itself.
    assert await brain.preflight() == "bad key"
    assert brain.opencode.preflights == 0

    brain.opencode.print = ("key-v2",)  # the operator replaced the key
    assert await brain.preflight() == ""
    assert brain.opencode.preflights == 1


@pytest.mark.asyncio
async def test_brain_usage_limit_starts_a_pause(state):
    brain = _opencode_brain(state, [OpenCodeError("allowance", kind="usage_limit")])
    assert await brain.think("hi") == ""
    assert "usage limit" in brain.paused_reason.lower()
    assert brain.opencode.calls == 1


@pytest.mark.asyncio
async def test_brain_retries_a_transient_failure(state, monkeypatch):
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0.0)
    result = OpenCodeResult(
        text="recovered", model="deepseek-v4.1-flash", request_id="req-2",
        duration_ms=10, cost_usd=0.0,
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    brain = _opencode_brain(state, [
        OpenCodeError("timeout", kind="timeout", retryable=True), result])
    assert await brain.think("hi") == "recovered"
    assert brain.opencode.calls == 2


# ── quota ──


@pytest.mark.asyncio
async def test_quota_windows_come_from_the_usage_ledger(state):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # $16 of a $80 model: 100% of the 5-hour allowance (20% of 80),
    # 40% of the weekly allowance (50% of 80), 20% of the month.
    await state.record_usage_event(
        provider="opencode", model="deepseek-v4.1-flash",
        cost_usd=16.0, created_at=now, output_tokens=10,
    )
    quota = OpenCodeQuotaClient(state, "deepseek-v4.1-flash")
    windows = await quota.get_utilization()
    assert windows["five_hour"]["utilization"] == 100.0
    assert windows["seven_day"]["utilization"] == 40.0
    assert windows["monthly"]["utilization"] == 20.0
    assert windows["monthly"]["resets_at"].endswith("T00:00:00+00:00")


@pytest.mark.asyncio
async def test_quota_monthly_limit_override_for_go_plus(state):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await state.record_usage_event(
        provider="opencode", model="deepseek-v4.1-flash",
        cost_usd=16.0, created_at=now, output_tokens=10,
    )
    windows = await OpenCodeQuotaClient(
        state, "deepseek-v4.1-flash", monthly_limit_usd=160,
    ).get_utilization()
    assert windows["five_hour"]["utilization"] == 50.0
    assert windows["seven_day"]["utilization"] == 20.0
    assert windows["monthly"]["utilization"] == 10.0


@pytest.mark.asyncio
async def test_unknown_model_has_no_quota(state):
    assert await OpenCodeQuotaClient(state, "omen-alpha").get_utilization() is None


# ── config ──


def test_setup_wizard_offers_opencode(monkeypatch):
    import harvey.setup as setup_mod

    monkeypatch.setattr(oc, "api_key_from_env", lambda: "test-key")
    answers = iter(["opencode", "deepseek-v4.1-flash"])
    monkeypatch.setattr(
        setup_mod, "_ask",
        lambda prompt, default="", required=True, secret=False: next(answers),
    )
    cfg = setup_mod._ask_brain_config()
    assert cfg == BrainConfig(provider="opencode", model="deepseek-v4.1-flash")


def test_brain_config_accepts_opencode_fields():
    cfg = BrainConfig(provider="opencode", model="deepseek-v4.1-flash",
                      base_url="https://opencode.ai/inference/go",
                      max_tokens=2048, monthly_limit_usd=160)
    assert cfg.provider == "opencode"
    assert cfg.max_tokens == 2048
    assert cfg.monthly_limit_usd == 160.0
    with pytest.raises(ValidationError):
        BrainConfig(provider="opencode", base_url="ftp://example.com")
    with pytest.raises(ValidationError):
        BrainConfig(provider="opencode", max_tokens=10)
