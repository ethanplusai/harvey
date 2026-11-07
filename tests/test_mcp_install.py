"""`sellbird mcp install|uninstall`: shown first, backed up, never clobbering.

Every test points the home folder, CODEX_HOME and `which` into tmp_path and
uses a fake runner, so nothing here can touch the real ~/.claude.json,
~/.codex/config.toml or run the real `claude mcp add`.
"""

import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from sellbird import mcp_install as mi

NOW = datetime(2026, 10, 24, 9, 30, 0)


def no_cli(_name):
    return None


class FakeRunner:
    """Records argv; never runs anything. Can play `claude mcp` on a file."""

    def __init__(self, returncode=0, claude_json: Path | None = None, fail_adds=0,
                 skip_write=False):
        self.calls = []
        self.returncode = returncode
        self.claude_json = claude_json
        self.fail_adds = fail_adds          # how many add-json calls fail first
        self.skip_write = skip_write        # exit 0 but don't touch the file

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        returncode = self.returncode
        if argv[1:3] == ["mcp", "add-json"] and self.fail_adds:
            self.fail_adds -= 1
            returncode = 1
        if self.claude_json is not None and returncode == 0 and not self.skip_write:
            doc = (json.loads(self.claude_json.read_text(encoding="utf-8"))
                   if self.claude_json.exists() else {})
            servers = doc.setdefault("mcpServers", {})
            if argv[1:3] == ["mcp", "add-json"]:
                servers[argv[5]] = {"type": "stdio", **json.loads(argv[6])}
            elif argv[1:3] == ["mcp", "remove"]:
                servers.pop(argv[3], None)
            self.claude_json.write_text(json.dumps(doc), encoding="utf-8")

        class Done:
            pass
        Done.returncode = returncode
        Done.stdout = ""
        Done.stderr = "boom" if returncode else ""
        return Done()


def backups(folder: Path):
    return sorted(p.name for p in folder.iterdir() if ".sellbird-backup-" in p.name)


# ── Claude Code ──

def test_claude_json_merge_keeps_other_servers(tmp_path):
    path = tmp_path / ".claude.json"
    other = {"command": "node", "args": ["x.js"]}
    path.write_text(json.dumps({"mcpServers": {"x": other}, "other": 1}), encoding="utf-8")

    change = mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner())
    assert change.action == "add"
    assert change.path == path
    assert all(line.startswith("+") for line in change.preview.splitlines())

    message = mi.apply_change(change, runner=FakeRunner(), now=NOW)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["other"] == 1
    assert doc["mcpServers"]["x"] == other
    assert doc["mcpServers"]["sellbird"] == mi.server_entry()
    assert backups(tmp_path) == [".claude.json.sellbird-backup-20261024093000"]
    assert "If Claude Code is open, restart it." in message


def test_second_install_is_a_no_op_without_backup(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"sellbird": mi.server_entry()}}),
                    encoding="utf-8")
    before = path.read_bytes()

    change = mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner())
    assert change.action == "none"
    assert mi.apply_change(change, runner=FakeRunner(), now=NOW) == \
        "Already set up for Claude Code."
    assert path.read_bytes() == before
    assert backups(tmp_path) == []


def test_invalid_json_is_never_rewritten(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_bytes(b'{"mcpServers": {broken')
    for which in (no_cli, lambda name: "/usr/local/bin/claude"):
        runner = FakeRunner()
        change = mi.plan_claude(home=tmp_path, which=which, runner=runner)
        assert change.action == "refuse"
        assert change.reason == "~/.claude.json isn't valid JSON, so it was left alone."
        assert mi.apply_change(change, runner=runner, now=NOW) == change.reason
        assert runner.calls == []
    assert path.read_bytes() == b'{"mcpServers": {broken'
    assert backups(tmp_path) == []


def test_missing_claude_is_refused(tmp_path):
    change = mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner())
    assert change.action == "refuse"
    assert change.reason == ("Claude Code isn't installed here (no `claude` command "
                             "and no ~/.claude.json).")
    assert not (tmp_path / ".claude.json").exists()


def test_foreign_claude_entry_is_refused(tmp_path):
    path = tmp_path / ".claude.json"
    foreign = {"command": "npx", "args": ["some-other-Sellbird"]}
    path.write_text(json.dumps({"mcpServers": {"sellbird": foreign}}), encoding="utf-8")
    before = path.read_bytes()
    for which in (no_cli, lambda name: "/usr/local/bin/claude"):
        runner = FakeRunner()
        change = mi.plan_claude(home=tmp_path, which=which, runner=runner)
        assert change.action == "refuse"
        mi.apply_change(change, runner=runner, now=NOW)
        assert runner.calls == []
        un = mi.plan_uninstall_claude(home=tmp_path, which=which, runner=runner)
        assert un.action == "refuse"
    assert path.read_bytes() == before


def test_older_entry_of_ours_is_replaced(tmp_path):
    path = tmp_path / ".claude.json"
    old = {"command": "/old/venv/bin/python", "args": ["-m", "sellbird", "mcp"]}
    path.write_text(json.dumps({"mcpServers": {"sellbird": old}}), encoding="utf-8")
    change = mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner())
    assert change.action == "replace"
    assert any(line.startswith("-") for line in change.preview.splitlines())
    mi.apply_change(change, runner=FakeRunner(), now=NOW)
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["sellbird"] == \
        mi.server_entry()


def test_claude_cli_preferred_when_present(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {}, "projects": {}}), encoding="utf-8")
    before = path.read_bytes()
    claude = str(tmp_path / "bin" / "claude")
    runner = FakeRunner(claude_json=path)

    change = mi.plan_claude(home=tmp_path, which=lambda name: claude, runner=runner)
    assert change.action == "cli"
    assert list(change.argv[:6]) == [claude, "mcp", "add-json", "--scope", "user",
                                     "sellbird"]
    assert json.loads(change.argv[6]) == mi.server_entry()
    assert claude in change.preview and "add-json" in change.preview
    assert runner.calls == []                       # planning never runs the CLI

    mi.apply_change(change, runner=runner, now=NOW)
    assert runner.calls == [list(change.argv)]      # only the CLI wrote the file
    assert backups(tmp_path) == [".claude.json.sellbird-backup-20261024093000"]
    assert (tmp_path / backups(tmp_path)[0]).read_bytes() == before


def test_claude_cli_replace_preview_shows_old_and_new(tmp_path):
    path = tmp_path / ".claude.json"
    old = {"command": "/old/bin/sellbird", "args": ["mcp"]}
    path.write_text(json.dumps({"mcpServers": {"sellbird": old}}), encoding="utf-8")
    change = mi.plan_claude(home=tmp_path, which=lambda name: "/bin/claude",
                            runner=FakeRunner())
    lines = change.preview.splitlines()
    assert any(l.startswith("-") and "/old/bin/sellbird" in l for l in lines)
    assert any(l.startswith("+") and json.dumps(sys.executable)[1:-1] in l for l in lines)
    assert "claude mcp remove sellbird --scope user" in change.preview


def test_claude_cli_add_failure_after_remove_puts_old_entry_back(tmp_path):
    path = tmp_path / ".claude.json"
    old = {"command": "/old/bin/sellbird", "args": ["mcp"]}
    path.write_text(json.dumps({"mcpServers": {"sellbird": old}}), encoding="utf-8")
    runner = FakeRunner(claude_json=path, fail_adds=1)
    change = mi.plan_claude(home=tmp_path, which=lambda name: "/bin/claude", runner=runner)
    with pytest.raises(mi.InstallError) as err:
        mi.apply_change(change, runner=runner, now=NOW)
    assert "put back" in str(err.value)
    assert [c[2] for c in runner.calls] == ["remove", "add-json", "add-json"]
    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["sellbird"]
    assert {k: v for k, v in entry.items() if k != "type"} == old


def test_claude_cli_both_adds_failing_names_the_backup(tmp_path):
    path = tmp_path / ".claude.json"
    old = {"command": "/old/bin/sellbird", "args": ["mcp"]}
    path.write_text(json.dumps({"mcpServers": {"sellbird": old}}), encoding="utf-8")
    runner = FakeRunner(claude_json=path, fail_adds=2)
    change = mi.plan_claude(home=tmp_path, which=lambda name: "/bin/claude", runner=runner)
    with pytest.raises(mi.InstallError) as err:
        mi.apply_change(change, runner=runner, now=NOW)
    backup = tmp_path / ".claude.json.sellbird-backup-20261024093000"
    assert "was removed" in str(err.value)
    assert str(backup) in str(err.value)
    assert json.loads(backup.read_text(encoding="utf-8"))["mcpServers"]["sellbird"] == old


def test_claude_cli_write_is_verified_by_rereading(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    runner = FakeRunner(claude_json=path, skip_write=True)
    change = mi.plan_claude(home=tmp_path, which=lambda name: "/bin/claude", runner=runner)
    with pytest.raises(mi.InstallError) as err:
        mi.apply_change(change, runner=runner, now=NOW)
    assert ".claude.json.sellbird-backup-20261024093000" in str(err.value)

    path.write_text(json.dumps({"mcpServers": {"sellbird": mi.server_entry()}}),
                    encoding="utf-8")
    un = mi.plan_uninstall_claude(home=tmp_path, which=lambda name: "/bin/claude",
                                  runner=runner)
    assert un.action == "cli" and un.preview.startswith("-")
    with pytest.raises(mi.InstallError):              # still there after "remove"
        mi.apply_change(un, runner=runner, now=NOW)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinked_configs_are_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    (tmp_path / ".claude.json").symlink_to(real)
    for which in (no_cli, lambda name: "/bin/claude"):
        change = mi.plan_claude(home=tmp_path, which=which, runner=FakeRunner())
        assert change.action == "refuse"
        assert change.reason.startswith("~/.claude.json is a symlink, so it was left alone.")
    real_toml = tmp_path / "real.toml"
    real_toml.write_text(CODEX_ORIGINAL, encoding="utf-8")
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").symlink_to(real_toml)
    change = mi.plan_codex(env=codex_env(tmp_path), home=tmp_path)
    assert change.action == "refuse"
    assert "is a symlink" in change.reason
    assert (tmp_path / ".claude.json").is_symlink()
    assert real.read_text(encoding="utf-8") == "{}"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_appearing_after_the_preview_is_refused(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    change = mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner())
    real = tmp_path / "real.json"
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(mi.InstallError, match="symlink"):
        mi.apply_change(change, runner=FakeRunner(), now=NOW)
    assert path.is_symlink()
    assert real.read_text(encoding="utf-8") == "{}"


def test_apply_time_errors_are_clean_and_write_nothing(tmp_path, monkeypatch):
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    change = mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner())
    path.write_text("{not json", encoding="utf-8")         # broken since the preview
    with pytest.raises(mi.InstallError) as err:
        mi.apply_change(change, runner=FakeRunner(), now=NOW)
    assert str(err.value) == mi.REFUSE_BAD_JSON
    assert path.read_text(encoding="utf-8") == "{not json"

    path.write_text("{}", encoding="utf-8")

    def no_backup(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(mi, "_backup_path", no_backup)
    with pytest.raises(mi.InstallError) as err:
        mi.apply_change(change, runner=FakeRunner(), now=NOW)
    assert "couldn't be read or backed up (Permission denied)" in str(err.value)
    assert path.read_text(encoding="utf-8") == "{}"


def test_backup_keeps_the_file_mode(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX modes")
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    mi.apply_change(mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner()), now=NOW)
    backup = tmp_path / backups(tmp_path)[0]
    assert backup.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600


def test_claude_cli_replace_removes_then_adds_and_is_then_a_no_op(tmp_path):
    path = tmp_path / ".claude.json"
    old = {"type": "stdio", "command": "/old/bin/sellbird", "args": ["mcp"]}
    path.write_text(json.dumps({"mcpServers": {"sellbird": old}}), encoding="utf-8")
    claude = "/usr/local/bin/claude"
    runner = FakeRunner(claude_json=path)

    change = mi.plan_claude(home=tmp_path, which=lambda name: claude, runner=runner)
    assert change.action == "cli"
    mi.apply_change(change, runner=runner, now=NOW)
    assert runner.calls[0] == [claude, "mcp", "remove", "sellbird", "--scope", "user"]
    assert runner.calls[1][:3] == [claude, "mcp", "add-json"]
    # The CLI stores "type": "stdio"; that still counts as the same entry.
    again = mi.plan_claude(home=tmp_path, which=lambda name: claude, runner=runner)
    assert again.action == "none"


def test_claude_cli_failure_is_reported(tmp_path):
    runner = FakeRunner(returncode=1)
    change = mi.plan_claude(home=tmp_path, which=lambda name: "/bin/claude", runner=runner)
    with pytest.raises(mi.InstallError):
        mi.apply_change(change, runner=runner, now=NOW)


def test_uninstall_claude_file_removes_only_ours(tmp_path):
    path = tmp_path / ".claude.json"
    other = {"command": "node", "args": ["x.js"]}
    path.write_text(json.dumps({"mcpServers": {"x": other, "sellbird": mi.server_entry()},
                                "other": 1}), encoding="utf-8")
    change = mi.plan_uninstall_claude(home=tmp_path, which=no_cli, runner=FakeRunner())
    assert change.action == "remove"
    mi.apply_change(change, runner=FakeRunner(), now=NOW)
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {"x": other},
                                                            "other": 1}
    assert mi.plan_uninstall_claude(home=tmp_path, which=no_cli,
                                    runner=FakeRunner()).action == "none"


def test_is_ours():
    assert mi.is_ours({"command": "/x/python", "args": ["-m", "sellbird", "mcp"]})  # before -P
    assert mi.is_ours({"command": "/x/python", "args": ["-P", "-m", "sellbird", "mcp"]})
    assert mi.is_ours({"command": "/x/bin/sellbird", "args": ["mcp"]})
    assert mi.is_ours({"command": "C:\\x\\Scripts\\sellbird.exe", "args": ["mcp"]})
    assert not mi.is_ours({"command": "npx", "args": ["sellbird"]})
    assert not mi.is_ours({"command": "/x/sellbird", "args": ["mcp", "--other"]})
    assert not mi.is_ours("not a dict")


def test_entry_has_no_token():
    entry = mi.server_entry()
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-P", "-m", "sellbird", "mcp"]
    assert set(entry["env"]) == {"SELLBIRD_HOME"}
    assert "token" not in json.dumps(entry).lower()


# ── Codex ──

CODEX_ORIGINAL = """# my codex settings
model = "gpt-5.5"   # keep this

[profiles.work]
model = 'o3'

[mcp_servers.other]
command = "node"
args = ["x.js"]
"""


def codex_env(tmp_path):
    return {"CODEX_HOME": str(tmp_path / "codex")}


def write_codex(tmp_path, text):
    path = tmp_path / "codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_codex_append_preserves_bytes_and_comments(tmp_path):
    path = write_codex(tmp_path, CODEX_ORIGINAL)
    change = mi.plan_codex(env=codex_env(tmp_path), home=tmp_path)
    assert change.action == "add"
    assert change.path == path
    assert mi.CODEX_MARKER in change.preview

    mi.apply_change(change, now=NOW)
    new = path.read_bytes().decode("utf-8")
    assert new.startswith(CODEX_ORIGINAL)
    doc = tomllib.loads(new)
    assert doc["mcp_servers"]["sellbird"] == mi.server_entry()
    assert doc["mcp_servers"]["other"] == {"command": "node", "args": ["x.js"]}
    assert backups(path.parent) == ["config.toml.sellbird-backup-20261024093000"]
    assert mi.plan_codex(env=codex_env(tmp_path), home=tmp_path).action == "none"


def test_backups_in_the_same_second_never_overwrite(tmp_path):
    path = write_codex(tmp_path, CODEX_ORIGINAL)
    mi.apply_change(mi.plan_codex(env=codex_env(tmp_path), home=tmp_path), now=NOW)
    mi.apply_change(mi.plan_uninstall_codex(env=codex_env(tmp_path), home=tmp_path), now=NOW)
    first = path.with_name("config.toml.sellbird-backup-20261024093000")
    assert first.read_text(encoding="utf-8") == CODEX_ORIGINAL
    assert path.with_name(first.name + "-2").exists()


def test_codex_missing_file_is_created(tmp_path):
    change = mi.plan_codex(env=codex_env(tmp_path), home=tmp_path)
    assert change.action == "add"
    mi.apply_change(change, now=NOW)
    doc = tomllib.loads((tmp_path / "codex" / "config.toml").read_text(encoding="utf-8"))
    assert doc == {"mcp_servers": {"sellbird": mi.server_entry()}}


def test_codex_windows_path_round_trips():
    entry = {"command": "C:\\Users\\Sam\\pipx\\venvs\\sellbird\\Scripts\\python.exe",
             "args": ["-m", "sellbird", "mcp"],
             "env": {"SELLBIRD_HOME": "C:\\Users\\Sam O'Neil\\AppData\\Roaming\\sellbird"}}
    block = mi.render_codex_block(entry)
    assert block.splitlines()[0] == mi.CODEX_MARKER
    assert block.splitlines()[1] == "[mcp_servers.sellbird]"
    assert "'C:\\Users\\Sam\\pipx" in block            # literal string, no escaping
    assert tomllib.loads(block)["mcp_servers"]["sellbird"] == entry


def test_codex_newline_value_round_trips():
    entry = {"command": "/a\"b\\c", "args": ["line\none"], "env": {"SELLBIRD_HOME": "x"}}
    assert tomllib.loads(mi.render_codex_block(entry))["mcp_servers"]["sellbird"] == entry


def test_invalid_toml_is_never_rewritten(tmp_path):
    path = write_codex(tmp_path, "model = [unclosed\n")
    change = mi.plan_codex(env=codex_env(tmp_path), home=tmp_path)
    assert change.action == "refuse"
    assert change.reason == "~/.codex/config.toml isn't valid TOML, so it was left alone."
    mi.apply_change(change, now=NOW)
    assert path.read_bytes() == b"model = [unclosed\n"


def test_foreign_codex_entry_is_refused(tmp_path):
    text = CODEX_ORIGINAL + "\n[mcp_servers.sellbird]\ncommand = 'npx'\n"
    path = write_codex(tmp_path, text)
    change = mi.plan_codex(env=codex_env(tmp_path), home=tmp_path)
    assert change.action == "refuse"
    assert change.reason == (
        "~/.codex/config.toml already has a Sellbird server that this command didn't "
        "add. Edit it yourself, or remove it and run this again.")
    mi.apply_change(change, now=NOW)
    assert mi.plan_uninstall_codex(env=codex_env(tmp_path), home=tmp_path).action == "refuse"
    assert path.read_bytes() == text.encode("utf-8")
    assert backups(path.parent) == []


def test_codex_marked_block_is_replaced_in_place(tmp_path):
    old = dict(mi.server_entry(), command="/old/python")
    text = CODEX_ORIGINAL + "\n" + mi.render_codex_block(old) + "\n# theirs\n[tail]\nk = 1\n"
    path = write_codex(tmp_path, text)
    change = mi.plan_codex(env=codex_env(tmp_path), home=tmp_path)
    assert change.action == "replace"
    mi.apply_change(change, now=NOW)
    new = path.read_text(encoding="utf-8")
    assert new.startswith(CODEX_ORIGINAL)
    assert new.endswith("\n# theirs\n[tail]\nk = 1\n")
    assert tomllib.loads(new)["mcp_servers"]["sellbird"] == mi.server_entry()


def test_codex_home_env_honored(tmp_path):
    custom = tmp_path / "elsewhere"
    change = mi.plan_codex(env={"CODEX_HOME": str(custom)}, home=tmp_path)
    assert change.path == custom / "config.toml"
    default = mi.plan_codex(env={}, home=tmp_path)
    assert default.path == tmp_path / ".codex" / "config.toml"


def test_uninstall_codex_removes_only_our_block(tmp_path):
    path = write_codex(tmp_path, CODEX_ORIGINAL)
    mi.apply_change(mi.plan_codex(env=codex_env(tmp_path), home=tmp_path), now=NOW)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write("\n# after ours\n[later]\nx = 'y'\n")

    change = mi.plan_uninstall_codex(env=codex_env(tmp_path), home=tmp_path)
    assert change.action == "remove"
    assert all(line.startswith("-") for line in change.preview.splitlines())
    mi.apply_change(change, now=datetime(2026, 10, 24, 9, 31, 0))
    new = path.read_text(encoding="utf-8")
    assert new == CODEX_ORIGINAL + "\n# after ours\n[later]\nx = 'y'\n"
    assert mi.plan_uninstall_codex(env=codex_env(tmp_path), home=tmp_path).action == "none"


def test_verify_failure_restores_original(tmp_path, monkeypatch):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"x": {"command": "a"}}}), encoding="utf-8")
    before = path.read_bytes()
    change = mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner())

    real_write = mi._write_atomic

    def lossy_write(target, text):          # drops another server: verification must catch it
        doc = json.loads(text)
        doc["mcpServers"].pop("x")
        real_write(target, json.dumps(doc))
    monkeypatch.setattr(mi, "_write_atomic", lossy_write)

    with pytest.raises(mi.InstallVerifyFailed) as err:
        mi.apply_change(change, runner=FakeRunner(), now=NOW)
    assert str(err.value) == "The change didn't check out, so your original file was restored."
    assert path.read_bytes() == before


def test_verify_failure_on_a_new_file_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, "_write_atomic",
                        lambda target, text: target.write_text("x = 1\n", encoding="utf-8"))
    change = mi.plan_codex(env=codex_env(tmp_path), home=tmp_path)
    with pytest.raises(mi.InstallVerifyFailed):
        mi.apply_change(change, now=NOW)
    assert not (tmp_path / "codex" / "config.toml").exists()


# ── The command ──

def run(tmp_path, *argv, which=no_cli, runner=None, tty=False, answer="n", env=None):
    out = []
    code = mi.run_command(
        argv[0], next((a for a in argv[1:] if a in ("claude", "codex")), None),
        print_only="--print" in argv, yes="--yes" in argv,
        home=tmp_path, env=codex_env(tmp_path) if env is None else env, which=which,
        runner=runner or FakeRunner(), isatty=lambda: tty, ask=lambda prompt: answer,
        out=out.append, now=NOW)
    return code, "\n".join(out)


def test_non_tty_without_yes_changes_nothing(tmp_path, capsys, monkeypatch):
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    code, text = run(tmp_path, "install")
    assert code == 2
    assert text.splitlines()[-1] == "Nothing was changed. Re-run with --yes to apply."
    assert path.read_text(encoding="utf-8") == "{}"
    assert backups(tmp_path) == []

    code, text = run(tmp_path, "install", "codex")
    assert code == 2
    assert not (tmp_path / "codex" / "config.toml").exists()


def test_yes_applies(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    code, text = run(tmp_path, "install", "--yes")
    assert code == 0
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["sellbird"] == \
        mi.server_entry()
    assert "If Claude Code is open, restart it." in text


def test_tty_asks_and_no_means_no(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    code, text = run(tmp_path, "install", tty=True, answer="")
    assert code == 1
    assert path.read_text(encoding="utf-8") == "{}"
    code, text = run(tmp_path, "install", tty=True, answer="y")
    assert code == 0
    assert "sellbird" in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_no_client_install_never_touches_codex(tmp_path):
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    codex = write_codex(tmp_path, CODEX_ORIGINAL)
    runner = FakeRunner()
    code, text = run(tmp_path, "install", "--yes", runner=runner,
                     which=lambda name: "/bin/codex" if name == "codex" else None)
    assert code == 0
    assert codex.read_bytes() == CODEX_ORIGINAL.encode("utf-8")
    assert runner.calls == []
    assert "Codex found. To add Sellbird to it too, run: sellbird mcp install codex" in text
    code, text = run(tmp_path, "uninstall", "--yes", runner=runner,
                     which=lambda name: "/bin/codex" if name == "codex" else None)
    assert code == 0
    assert codex.read_bytes() == CODEX_ORIGINAL.encode("utf-8")


def test_refusal_exits_nonzero_and_says_why(tmp_path):
    code, text = run(tmp_path, "install", "--yes")
    assert code == 1
    assert "Claude Code isn't installed here" in text


def test_already_set_up_exits_zero_without_asking(tmp_path):
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"sellbird": mi.server_entry()}}), encoding="utf-8")
    code, text = run(tmp_path, "install")
    assert code == 0
    assert "Already set up for Claude Code." in text


def test_plain_sellbird_mcp_still_serves_stdio(monkeypatch, capsys):
    import sellbird.cli as cli
    import sellbird.home as home
    import sellbird.mcp_server as server

    served = []
    monkeypatch.setattr(server, "run_stdio", lambda: served.append(True))
    monkeypatch.setattr(home, "seed_home", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["sellbird", "mcp"])
    cli.main()
    assert served == [True]
    out = capsys.readouterr()
    assert out.out == ""


def test_cli_routes_install(monkeypatch, capsys):
    import sellbird.cli as cli
    import sellbird.home as home

    seen = {}

    def fake_run_command(action, client, *, print_only, yes):
        seen.update(action=action, client=client, print_only=print_only, yes=yes)
        return 0
    monkeypatch.setattr(mi, "run_command", fake_run_command)
    monkeypatch.setattr(home, "seed_home", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["sellbird", "mcp", "install", "codex", "--yes"])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code == 0
    assert seen == {"action": "install", "client": "codex", "print_only": False, "yes": True}


@pytest.mark.parametrize("argv", [["sellbird", "mcp", "--yes"], ["sellbird", "mcp", "--print"]])
def test_flags_without_an_action_are_an_error(monkeypatch, capsys, argv):
    import sellbird.cli as cli
    import sellbird.home as home
    import sellbird.mcp_server as server

    served = []
    monkeypatch.setattr(server, "run_stdio", lambda: served.append(True))
    monkeypatch.setattr(home, "seed_home", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code == 2
    assert served == []
    out = capsys.readouterr()
    assert out.out == ""
    assert "go with install or uninstall" in out.err


def test_print_config_writes_nothing(tmp_path, capsys):
    code, text = run(tmp_path, "install", "--print")
    assert code == 0
    assert list(tmp_path.iterdir()) == []
    config = mi.print_config()
    assert text == config
    assert '"mcpServers"' in config and mi.CODEX_MARKER in config
    assert config.splitlines()[-1] == (
        f"Any other MCP client: command {sys.executable}, args -P -m sellbird mcp, "
        f"env SELLBIRD_HOME={mi.server_entry()['env']['SELLBIRD_HOME']}")
    first_json = config.split("\n\n")[0]
    assert json.loads(first_json) == {"mcpServers": {"sellbird": mi.server_entry()}}


def test_brain_codex_home_is_still_private():
    from sellbird.codex import build_private_config_toml

    text = build_private_config_toml()
    assert "sellbird" not in text
    assert "mcp_servers" not in text


# ── private temp and backup files, parse before backup ──

def test_bad_json_at_apply_time_leaves_no_backup(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    change = mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner())
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(mi.InstallError):
        mi.apply_change(change, runner=FakeRunner(), now=NOW)
    assert backups(tmp_path) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_backup_and_restore_files_are_created_private(tmp_path, monkeypatch):
    """Created 0o600 whatever the umask, before the original's mode is copied."""
    import os
    import shutil
    path = tmp_path / ".claude.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(shutil, "copymode", lambda src, dst: None)  # see the creation mode
    old = os.umask(0)
    try:
        mi.apply_change(mi.plan_claude(home=tmp_path, which=no_cli, runner=FakeRunner()),
                        now=NOW)
        backup = tmp_path / backups(tmp_path)[0]
        assert backup.stat().st_mode & 0o777 == 0o600
        target = tmp_path / "restored.json"
        mi._restore(b"{}", backup, target)
        assert target.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_restore_keeps_the_originals_mode(tmp_path):
    backup = tmp_path / "b"
    backup.write_bytes(b"{}")
    backup.chmod(0o640)
    target = tmp_path / "t.json"
    mi._restore(b'{"a": 1}', backup, target)
    assert target.read_bytes() == b'{"a": 1}' and target.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".t.json.*"))


def test_cli_remove_with_an_unreadable_file_afterwards_is_not_success(tmp_path, monkeypatch):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"mcpServers": {"sellbird": mi.server_entry()}}),
                    encoding="utf-8")
    claude = str(tmp_path / "bin" / "claude")
    runner = FakeRunner(claude_json=path)
    change = mi.plan_uninstall_claude(home=tmp_path, which=lambda name: claude, runner=runner)
    assert change.action == "cli"
    monkeypatch.setattr(mi, "_read_claude", lambda p: (None, "unreadable"))
    with pytest.raises(mi.InstallError) as err:
        mi.apply_change(change, runner=runner, now=NOW)
    assert "didn't end up as expected" in str(err.value)
