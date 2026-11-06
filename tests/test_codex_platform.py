"""Platform-sensitive paths of the Codex adapter.

Two kinds of test, kept apart on purpose:

* Real behaviour on whatever OS runs the suite: long and non-ASCII prompts
  over stdio, paths with spaces and non-ASCII characters, launching a real
  executable from such a path, private file modes (POSIX).
* Windows: the Codex provider is unsupported there (macOS and Linux only).
  These tests flip ``sellbird.codex._PLATFORM`` to check that every public
  Codex entry point refuses it before reading the login or starting any
  process, and that Claude is unaffected. They run on every OS.
"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

import sellbird.codex as hc
from tests.test_codex import (  # noqa: F401  (pytest fixtures)
    FAKE, PROMPT, _client, _write_login, fake, native_home, private_tempdir,
)

POSIX = os.name == "posix"
posix_only = pytest.mark.skipif(not POSIX, reason="the Codex provider runs on macOS/Linux only")


# ── real behaviour on this OS ──


@posix_only
@pytest.mark.asyncio
async def test_long_non_ascii_prompt_reaches_codex_intact(fake, native_home):
    """Prompts go over the stdio pipe, never argv, so length and encoding
    don't matter (the Windows command-line limit never applies)."""
    prompt = ("Prospect wrote: Grüße aus Köln — 東京の件、ありがとう 🙏. " * 4000)[:200_000]
    await _client(native_home).run(prompt, timeout=60)
    turn = next(e for e in fake.entries() if "prompt_sha" in e)
    assert turn["prompt_chars"] == len(prompt)
    assert turn["prompt_has_non_ascii"] is True
    assert turn["prompt_sha"] == hashlib.sha256(prompt.encode()).hexdigest()
    server = next(e for e in fake.entries() if e.get("cmd") == "app-server")
    assert server["argv"] == ["app-server", "--listen", "stdio://"]


@posix_only
@pytest.mark.asyncio
async def test_paths_with_spaces_and_non_ascii(fake, tmp_path, monkeypatch):
    home = tmp_path / "Ünïcode home" / "with space"
    home.mkdir(parents=True)
    _write_login(home)
    runtimes = tmp_path / "temp dir ü 東京"
    runtimes.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(runtimes))
    result = await _client(home).run(PROMPT, timeout=30)
    assert result.text
    server = fake.servers()[1]
    assert server["codex_home"].startswith(str(runtimes))
    assert list(runtimes.iterdir()) == []  # cleaned up


@pytest.mark.asyncio
@pytest.mark.skipif(not POSIX, reason="POSIX wrapper script; Windows uses a .cmd shim")
async def test_real_executable_in_a_path_with_spaces(native_home, tmp_path, monkeypatch):
    """No routing fixture: Sellbird execs a real file whose path has spaces and
    non-ASCII, found through PATH like the npm `codex` shim."""
    bindir = tmp_path / "Program Files ü" / "codex bin"
    bindir.mkdir(parents=True)
    log = tmp_path / "wrapper.log"
    wrapper = bindir / "codex"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{FAKE}" ok "{log}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert hc.find_codex() == str(wrapper)
    result = await hc.CodexClient(native_home=native_home).run(PROMPT, timeout=30)
    assert result.text == '{"intent": "interested"}'


@pytest.mark.asyncio
@pytest.mark.skipif(not POSIX, reason="POSIX file modes")
async def test_private_runtime_and_login_copy_modes(fake, native_home):
    await _client(native_home).run(PROMPT, timeout=30)
    for server in fake.servers():
        assert server["home_mode"] == 0o700
        assert server["codex_home_mode"] == 0o700
        assert server["workspace_mode"] == 0o700
    assert fake.servers()[1]["auth_mode_bits"] == 0o600


def test_environment_allowlist_is_case_insensitive_and_drops_secrets():
    env = hc.build_child_env("/p", "/p/h", base_env={
        "Path": "C:\\bin", "PATHEXT": ".COM;.EXE;.CMD", "SystemRoot": "C:\\Windows",
        "ComSpec": "cmd.exe", "OPENAI_API_KEY": "sk-x", "openai_base_url": "https://x",
        "CODEX_HOME": "C:\\real", "GITHUB_TOKEN": "t", "AWS_PROFILE": "p",
        "HTTPS_PROXY": "http://proxy:8080", "LC_CTYPE": "UTF-8",
    })
    assert {"Path", "PATHEXT", "SystemRoot", "ComSpec", "HTTPS_PROXY", "LC_CTYPE"} <= set(env)
    for dropped in ("OPENAI_API_KEY", "openai_base_url", "GITHUB_TOKEN", "AWS_PROFILE"):
        assert dropped not in env
    assert env["CODEX_HOME"] == "/p/h" and env["HOME"] == "/p"


# ── Windows: unsupported for Codex, refused up front (runs on every OS) ──


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(hc, "_PLATFORM", "win32")

    def no_login_read(*a, **k):
        raise AssertionError("the native login must not be read on an unsupported platform")

    monkeypatch.setattr(hc.Path, "read_text", no_login_read)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["run", "read_rate_limits", "preflight"])
async def test_windows_codex_is_refused_before_login_or_runtime(fake, native_home, on_windows,
                                                                surface):
    client = _client(native_home)
    call = {"run": lambda: client.run(PROMPT, timeout=30),
            "read_rate_limits": lambda: client.read_rate_limits(timeout=30),
            "preflight": lambda: client.preflight(timeout=30)}[surface]
    with pytest.raises(hc.CodexError) as err:
        await call()
    assert err.value.kind == "setup" and not err.value.retryable
    assert "macOS and Linux" in str(err.value) and "brain.provider: claude" in str(err.value)
    assert fake.entries() == []  # no CLI process of any kind


def test_windows_setup_checks_refuse_codex(native_home, on_windows):
    for check in (lambda: hc.check_native_auth(native_home), lambda: hc.check_cli_version("codex")):
        with pytest.raises(hc.CodexError) as err:
            check()
        assert "macOS and Linux" in str(err.value)


@pytest.mark.asyncio
async def test_windows_quota_and_brain_pause_cleanly(on_windows, tmp_path):
    import json as _json

    from sellbird.brain import BRAIN_STATUS_KEY, Brain
    from sellbird.config import BrainConfig
    from sellbird.state import StateManager

    state = StateManager(str(tmp_path / "w.db"))
    await state.init_db()
    brain = Brain(state, BrainConfig(provider="codex", model="gpt-5.5"))
    assert await brain.quota.get_utilization() is None
    problem = await brain.preflight()
    assert "macOS and Linux" in problem
    stored = _json.loads(await state.get_setting(BRAIN_STATUS_KEY))
    assert stored["status"] == "paused" and stored["kind"] == "setup"
    assert await brain.think("hi") == ""


def test_windows_setup_offers_claude_instead(monkeypatch, on_windows, capsys):
    import sellbird.setup as setup_mod
    from sellbird.config import BrainConfig

    answers = iter(["codex", "claude", ""])
    monkeypatch.setattr(setup_mod, "_ask", lambda *a, **k: next(answers))
    assert setup_mod._ask_brain_config() == BrainConfig()
    assert "macOS and Linux" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_claude_is_unaffected_on_windows(on_windows, monkeypatch, tmp_path):
    import sellbird.brain as brain_mod
    from sellbird.brain import Brain
    from sellbird.state import StateManager

    class Proc:
        returncode = 0

        async def communicate(self, data=None):
            return b'{"type": "result", "result": "ok", "session_id": "s"}', b""

    async def fake_claude(*cmd, **kwargs):
        assert cmd[0] == "claude"
        return Proc()

    monkeypatch.setattr(brain_mod.asyncio, "create_subprocess_exec", fake_claude)
    state = StateManager(str(tmp_path / "c.db"))
    await state.init_db()
    brain = Brain(state)
    assert await brain.preflight() == ""
    assert await brain.think("hello") == "ok"
