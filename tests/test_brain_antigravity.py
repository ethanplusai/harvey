"""Tests for the Antigravity subscription brain provider."""

import asyncio
import json
import os
import tempfile
import pytest
import pytest_asyncio
from pydantic import ValidationError

import harvey.brain as brain_mod
from harvey.brain import Brain
from harvey.config import BrainConfig, HarveyConfig
from harvey.state import StateManager

BASE = {
    "persona": {"name": "H", "company": "A", "role": "B", "email": "e", "linkedin": "", "tone": "t"},
    "product": {"name": "P", "description": "D", "pricing": "$1", "key_benefits": [], "objection_responses": {}},
    "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
}


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class _FakeAgyProcess:
    returncode = 0

    def __init__(self, response_text="Hello from Antigravity", returncode=0, stderr=b""):
        self.stdin_data = None
        self.response_text = response_text
        self.returncode = returncode
        self.stderr = stderr

    async def communicate(self, data=None):
        self.stdin_data = data
        if self.returncode != 0:
            return b"", self.stderr
        payload = {
            "conversation_id": "test-convo-123",
            "status": "SUCCESS",
            "response": self.response_text,
            "duration_seconds": 1.25,
            "num_turns": 1,
            "usage": {
                "input_tokens": 120,
                "output_tokens": 40,
                "thinking_tokens": 10,
                "cache_read_tokens": 0,
                "total_tokens": 160,
            },
        }
        return json.dumps(payload).encode("utf-8"), b""


def _capture_agy_exec(monkeypatch, response_text="Hello from Antigravity", returncode=0, stderr=b""):
    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        captured["proc"] = _FakeAgyProcess(response_text, returncode=returncode, stderr=stderr)
        return captured["proc"]

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    return captured


def test_antigravity_brain_config():
    cfg = BrainConfig(provider="antigravity", model="gemini-3.8-flash-high", reasoning_effort="max")
    assert cfg.provider == "antigravity"
    assert cfg.model == "gemini-3.8-flash-high"
    assert cfg.reasoning_effort == "max"

    with pytest.raises(ValidationError):
        BrainConfig(provider="antigravity", reasoning_effort="invalid_effort")


@pytest.mark.asyncio
async def test_antigravity_brain_runs_agy_with_flags(state, monkeypatch):
    captured = _capture_agy_exec(monkeypatch)
    cfg = BrainConfig(provider="antigravity", model="gemini-3.8-flash-high", reasoning_effort="high")
    brain = Brain(state, cfg)

    result = await brain.think("Draft a cold email", session_id="test-session")
    assert result == "Hello from Antigravity"

    cmd = captured["cmd"]
    assert cmd[0] == "agy"
    assert "-p" in cmd
    assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "json"
    assert "--disable-slash-commands" in cmd
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-high"
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "high"

    # Security: ensure dangerous flags are never added
    assert "--dangerously-skip-permissions" not in cmd

    # Verify usage records
    assert await state.get_usage_today("antigravity") == 1
    rows = await state.usage_by_provider(days=1)
    assert any(r["provider"] == "antigravity" for r in rows)


@pytest.mark.asyncio
async def test_antigravity_long_prompt_uses_stdin(state, monkeypatch):
    captured = _capture_agy_exec(monkeypatch)
    cfg = BrainConfig(provider="antigravity")
    brain = Brain(state, cfg)

    long_prompt = "P" * (brain_mod._MAX_ARGV_PROMPT_CHARS + 1)
    await brain.think(long_prompt)

    cmd = captured["cmd"]
    assert long_prompt not in cmd
    assert "--input-format" in cmd and cmd[cmd.index("--input-format") + 1] == "text"
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert captured["proc"].stdin_data == long_prompt.encode("utf-8")


@pytest.mark.asyncio
async def test_antigravity_preflight_checks_binary(state, monkeypatch):
    brain = Brain(state, BrainConfig(provider="antigravity"))

    monkeypatch.setattr(brain_mod.shutil, "which", lambda cmd: "/usr/local/bin/agy" if cmd == "agy" else None)
    assert await brain.preflight() == ""

    monkeypatch.setattr(brain_mod.shutil, "which", lambda cmd: None)
    error = await brain.preflight()
    assert "not found" in error.lower()


@pytest.mark.asyncio
async def test_antigravity_cli_missing(state, monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("agy not found")

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    brain = Brain(state, BrainConfig(provider="antigravity"))
    result = await brain.think("test")
    assert result == ""


@pytest.mark.asyncio
async def test_antigravity_auth_error_not_retryable(state, monkeypatch):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return _FakeAgyProcess(returncode=1, stderr=b"Error: Not logged in. Please run `agy login`.")

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_exec)
    brain = Brain(state, BrainConfig(provider="antigravity"))
    result = await brain.think("test", max_retries=2)
    assert result == ""
    # Should exit immediately on non-retryable auth error without looping retries
    assert len(calls) == 1
