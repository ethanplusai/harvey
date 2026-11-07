"""Tests for the Kimi Code CLI brain runtime (sellbird/kimi_runtime.py)."""

import os

import pytest

from sellbird import kimi_runtime
from sellbird.kimi_runtime import (
    KimiRuntimeError,
    build_command,
    check_cli,
    missing_flags,
    parse_stream_json,
)


SAMPLE_HELP = """\
Usage: kimi [options]
  -p, --prompt <prompt>   Run a single prompt non-interactively
  --output-format <format>
  --agent-file <path>     Load a custom agent from a Markdown file
"""


# ── parse_stream_json ──


def test_parse_stream_json_concatenates_assistant_lines():
    raw = (
        '{"role":"meta","type":"system.version","version":"2.5.2"}\n'
        '{"role":"assistant","content":"Hello "}\n'
        '{"role":"assistant","content":"world"}\n'
        '{"role":"meta","type":"session.resume_hint","session_id":"s-1"}\n'
    )
    text, payload = parse_stream_json(raw)
    assert text == "Hello world"
    assert payload["is_error"] is False
    assert payload["session_id"] == "s-1"
    assert payload["num_turns"] == 1


def test_parse_stream_json_handles_block_content_and_errors():
    raw = (
        '{"role":"assistant","content":[{"type":"text","text":"A"}]}\n'
        '{"role":"meta","type":"api.error","detail":"boom"}\n'
        "not json at all\n"
        '{"unexpected":true}\n'
    )
    text, payload = parse_stream_json(raw)
    assert text == "A"
    assert payload["is_error"] is True


def test_parse_stream_json_empty_output_is_not_an_error_turn():
    text, payload = parse_stream_json("")
    assert text == ""
    assert payload["num_turns"] == 0


# ── flag checking (monkeypatched, the conftest stand-in idiom: no subprocess) ──


def test_missing_flags_with_a_capable_help():
    assert missing_flags(SAMPLE_HELP) == []


def test_missing_flags_lists_absent_ones():
    missing = missing_flags("Usage: kimi\n  -p, --prompt <prompt>\n")
    assert "--output-format" in missing
    assert "--agent-file" in missing


def _stub_cli(monkeypatch, help_text):
    monkeypatch.setattr(kimi_runtime, "find_kimi", lambda env=None: "/usr/bin/kimi")
    monkeypatch.setattr(kimi_runtime, "_probe_help", lambda exe: help_text)


def test_check_cli_accepts_a_flag_capable_cli(monkeypatch):
    _stub_cli(monkeypatch, SAMPLE_HELP)
    assert check_cli({"PATH": "/nonexistent"}) == "/usr/bin/kimi"


def test_check_cli_refuses_a_cli_missing_flags(monkeypatch):
    _stub_cli(monkeypatch, "Usage: kimi\n  -p, --prompt <prompt>\n")
    with pytest.raises(KimiRuntimeError, match="lacks"):
        check_cli({"PATH": "/nonexistent"})


def test_check_cli_reports_when_kimi_is_absent(tmp_path, monkeypatch):
    # Override the conftest stand-in: this test wants PATH lookup to fail.
    monkeypatch.setattr(kimi_runtime, "find_kimi", lambda env=None: None)
    with pytest.raises(KimiRuntimeError, match="not found"):
        check_cli({"PATH": str(tmp_path)})


# ── command construction ──


def test_build_command_is_tool_less_and_print_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(kimi_runtime, "kimi_home", lambda: tmp_path)
    agent = tmp_path / "sellbird-brain.md"
    cmd = build_command("/usr/bin/kimi", "the prompt", model="kimi-code/k3")
    assert cmd[0] == "/usr/bin/kimi"
    assert cmd[cmd.index("-p") + 1] == "the prompt"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--agent-file") + 1] == str(agent)
    assert cmd[cmd.index("--model") + 1] == "kimi-code/k3"


def test_build_command_omits_model_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(kimi_runtime, "kimi_home", lambda: tmp_path)
    cmd = build_command("/usr/bin/kimi", "hi")
    assert "--model" not in cmd


# ── isolated home ──


def test_ensure_home_links_credentials_and_copies_config(tmp_path, monkeypatch):
    real = tmp_path / "real-home"
    (real / "credentials").mkdir(parents=True)
    (real / "config.toml").write_text('default_model = "x/y"\n', encoding="utf-8")
    target = tmp_path / "kimi-home"
    monkeypatch.setattr(kimi_runtime, "_operator_home", lambda: real)
    monkeypatch.setattr(kimi_runtime, "kimi_home", lambda: target)

    kimi_runtime.ensure_home()

    if os.name == "nt":
        assert (target / "credentials").is_dir()
        assert (target / "credentials").resolve() != (real / "credentials").resolve()
    else:
        assert (target / "credentials").is_symlink()
        assert (target / "credentials").resolve() == (real / "credentials").resolve()
    assert (target / "config.toml").read_text(encoding="utf-8") == 'default_model = "x/y"\n'
    agent = (target / "sellbird-brain.md").read_text(encoding="utf-8")
    assert "tools: []" in agent


def test_ensure_home_copies_credentials_when_symlinks_unavailable(tmp_path, monkeypatch):
    import pathlib

    real = tmp_path / "real-home"
    (real / "credentials").mkdir(parents=True)
    (real / "credentials" / "cred.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "kimi-home"
    monkeypatch.setattr(kimi_runtime, "_operator_home", lambda: real)
    monkeypatch.setattr(kimi_runtime, "kimi_home", lambda: target)

    def no_symlinks(self, *args, **kwargs):
        raise OSError("symbolic links are not permitted")

    monkeypatch.setattr(pathlib.Path, "symlink_to", no_symlinks)
    kimi_runtime.ensure_home()

    assert (target / "credentials" / "cred.json").is_file()


def test_ensure_home_seeds_a_minimal_config_without_an_operator_one(tmp_path, monkeypatch):
    real = tmp_path / "real-home"  # no config.toml, no credentials
    target = tmp_path / "kimi-home"
    monkeypatch.setattr(kimi_runtime, "_operator_home", lambda: real)
    monkeypatch.setattr(kimi_runtime, "kimi_home", lambda: target)

    kimi_runtime.ensure_home()

    config = (target / "config.toml").read_text(encoding="utf-8")
    assert "managed:kimi-code" in config
    assert not (target / "credentials").exists()


# ── provider wiring (config, labels, usage ledger) ──


def test_brain_config_accepts_kimi_and_validates_model():
    from sellbird.config import BrainConfig

    assert BrainConfig(provider="KIMI").provider == "kimi"
    assert BrainConfig(provider="kimi", model="kimi-code/k3").model == "kimi-code/k3"
    with pytest.raises(Exception, match="not a supported brain provider"):
        BrainConfig(provider="gemini")


def test_provider_labels_include_kimi():
    from sellbird.brain import PROVIDER_LABELS

    assert PROVIDER_LABELS["kimi"] == "Kimi"


@pytest.mark.asyncio
async def test_kimi_counts_in_the_usage_ledger(tmp_path):
    from sellbird.state import StateManager

    sm = StateManager(db_path=str(tmp_path / "s.db"))
    await sm.init_db()
    await sm.increment_usage(provider="kimi")
    await sm.increment_usage(provider="kimi")
    assert await sm.get_usage_today("kimi") == 2
    assert await sm.get_usage_today("claude") == 0
    assert await sm.record_usage_event(provider="kimi", agent="writer", task="t") is True
    with pytest.raises(ValueError, match="unknown brain provider"):
        await sm.record_usage_event(provider="gemini", agent="writer", task="t")
