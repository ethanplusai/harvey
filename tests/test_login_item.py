"""Start at login: a LaunchAgent on macOS, a Startup-folder shortcut on Windows.

Nothing here touches the real ~/Library/LaunchAgents, the real launchctl or a
real Startup folder: `home` and `APPDATA` point into tmp_path and the runner is
a fake that records argv and env instead of running anything.
"""

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from sellbird.desktop import login_item as li
from sellbird.desktop.login_item import (
    LABEL,
    MARKER,
    PLIST_COMMENT,
    WINDOWS_SHORTCUT,
    ForeignLoginItem,
    UnsupportedPlatform,
)


class FakeRunner:
    """Records every call; answers with a configurable CompletedProcess."""

    def __init__(self, returncode=0, stderr="", stdout="", on_call=None):
        self.calls = []
        self.returncode, self.stderr, self.stdout = returncode, stderr, stdout
        self.on_call = on_call

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
        if self.on_call:
            result = self.on_call(argv, kwargs)
            if result is not None:
                return result
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.fixture(autouse=True)
def _project_root(tmp_path, monkeypatch):
    # The log path lives under PROJECT_ROOT/data; never the checkout's data/.
    monkeypatch.setattr(li, "PROJECT_ROOT", tmp_path / "project")


def _mac_env(tmp_path, **extra):
    return {"PATH": "/opt/homebrew/bin:/usr/bin:/bin", **extra}


def _mac_install(tmp_path, runner=None, **kw):
    runner = runner or FakeRunner()
    kw.setdefault("sleep", lambda seconds: None)
    result = li.install(platform="darwin", runner=runner, env=kw.pop("env", _mac_env(tmp_path)),
                        home=tmp_path / "home", **kw)
    return result, runner


# ── macOS ──────────────────────────────────────────────────────────────────

def test_plist_carries_install_time_path(tmp_path):
    env = _mac_env(tmp_path, PATH="/Users/me/.local/bin:/opt/homebrew/bin:/usr/bin:/bin")
    result, _ = _mac_install(tmp_path, env=env)
    data = plistlib.loads(result.path.read_bytes())
    assert data["EnvironmentVariables"]["PATH"] == env["PATH"]
    assert "SELLBIRD_HOME" not in data["EnvironmentVariables"]


def test_plist_carries_sellbird_home_when_set(tmp_path):
    env = _mac_env(tmp_path, SELLBIRD_HOME=str(tmp_path / "ob-home"))
    result, _ = _mac_install(tmp_path, env=env)
    data = plistlib.loads(result.path.read_bytes())
    assert data["EnvironmentVariables"]["SELLBIRD_HOME"] == str((tmp_path / "ob-home").resolve())


def test_plist_is_valid_and_marked(tmp_path):
    log = tmp_path / "tray.log"
    data = li.render_plist(["/py", "-m", "sellbird", "tray"], "/usr/bin:/bin", None, log)
    parsed = plistlib.loads(data)
    assert parsed["Label"] == LABEL
    assert PLIST_COMMENT.encode() in data
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["ProcessType"] == "Interactive"
    assert parsed["LimitLoadToSessionType"] == "Aqua"
    assert parsed["ProgramArguments"] == ["/py", "-m", "sellbird", "tray"]
    assert parsed["StandardOutPath"] == parsed["StandardErrorPath"] == str(log)
    assert parsed["WorkingDirectory"] == str(li.PROJECT_ROOT)
    lines = data.decode("utf-8").splitlines()
    doctype = next(i for i, line in enumerate(lines) if line.startswith("<!DOCTYPE"))
    assert lines[doctype + 1] == PLIST_COMMENT
    assert li.is_ours_plist(data)


def test_install_bootstraps_with_absolute_launchctl(tmp_path):
    result, runner = _mac_install(tmp_path)
    uid = li._uid()
    path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert result.path == path and path.is_file()
    assert result.changed is True
    assert result.message == "Sellbird starts at login, and is starting now."
    assert [c["argv"] for c in runner.calls] == [
        ["/bin/launchctl", "bootout", f"gui/{uid}/{LABEL}"],
        ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(path)],
    ]
    data = plistlib.loads(path.read_bytes())
    assert data["StandardOutPath"] == str(tmp_path / "project" / "data" / "tray.log")
    assert data["ProgramArguments"] == li.tray_argv("darwin")


def test_install_writes_port_into_program_arguments(tmp_path):
    # F-31: `sellbird autostart install --port N` -> the tray gets `--port N`.
    result, _ = _mac_install(tmp_path, port=5596)
    args = plistlib.loads(result.path.read_bytes())["ProgramArguments"]
    assert args[-4:] == ["sellbird", "tray", "--port", "5596"]


def test_failed_bootstrap_still_saves(tmp_path):
    runner = FakeRunner(returncode=5, stderr="Bootstrap failed: 5: Input/output error\n")
    waits = []
    result, _ = _mac_install(tmp_path, runner=runner, sleep=waits.append)
    assert [c["argv"][1] for c in runner.calls] == ["bootout", "bootstrap", "bootstrap"]
    assert waits == [li.BOOTSTRAP_RETRY_WAIT]
    assert result.changed is True and result.path.is_file()
    assert result.message == ("Saved, but macOS didn't load it (Bootstrap failed: 5: "
                              "Input/output error). It will start at your next login.")


def test_bootstrap_retry_after_bootout_race(tmp_path):
    # bootout returns before launchd finishes; the first bootstrap can lose the race.
    answers = iter([0, 5, 0])   # bootout, bootstrap (fails), bootstrap (ok)
    runner = FakeRunner(on_call=lambda argv, kw: subprocess.CompletedProcess(
        argv, next(answers), "", "Bootstrap failed: 5: Input/output error"))
    waits = []
    result, _ = _mac_install(tmp_path, runner=runner, sleep=waits.append)
    assert waits == [li.BOOTSTRAP_RETRY_WAIT]
    assert result.message == "Sellbird starts at login, and is starting now."


def test_install_twice_is_a_no_op(tmp_path):
    first, runner = _mac_install(tmp_path)
    before = len(runner.calls)
    second, _ = _mac_install(tmp_path, runner=runner)
    assert first.changed is True
    assert second.changed is False
    assert second.message == "Already starts at login."
    assert len(runner.calls) == before


def test_install_rewrites_our_own_stale_plist(tmp_path):
    _mac_install(tmp_path, env=_mac_env(tmp_path, PATH="/old/bin"))
    result, runner = _mac_install(tmp_path, env=_mac_env(tmp_path, PATH="/new/bin"))
    assert result.changed is True
    assert plistlib.loads(result.path.read_bytes())["EnvironmentVariables"]["PATH"] == "/new/bin"
    assert runner.calls[-1]["argv"][1] == "bootstrap"


def _foreign_plist(path: Path, label=LABEL, comment=False) -> bytes:
    data = plistlib.dumps({"Label": label, "ProgramArguments": ["/usr/bin/true"]})
    if comment:
        data = data.replace(b"<plist", PLIST_COMMENT.encode() + b"\n<plist", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def test_foreign_plist_is_refused_and_untouched(tmp_path):
    path = li.login_item_path("darwin", home=tmp_path / "home")
    original = _foreign_plist(path)                  # our label, no comment
    runner = FakeRunner()
    with pytest.raises(ForeignLoginItem) as err:
        _mac_install(tmp_path, runner=runner)
    assert str(err.value) == f"{path} exists and wasn't created by Sellbird. Nothing was changed."
    assert path.read_bytes() == original
    assert runner.calls == []


def test_comment_with_another_label_is_not_ours(tmp_path):
    path = li.login_item_path("darwin", home=tmp_path / "home")
    data = _foreign_plist(path, label="com.someone.else", comment=True)
    assert not li.is_ours_plist(data)
    assert not li.is_ours_plist(b"not a plist " + PLIST_COMMENT.encode())


def test_uninstall_removes_only_ours(tmp_path):
    home = tmp_path / "home"
    _mac_install(tmp_path)
    runner = FakeRunner()
    result = li.uninstall(platform="darwin", runner=runner, env=_mac_env(tmp_path), home=home)
    assert result.changed is True and not result.path.exists()
    assert [c["argv"] for c in runner.calls] == [
        ["/bin/launchctl", "bootout", f"gui/{li._uid()}/{LABEL}"]]

    nothing_loaded = FakeRunner(returncode=113, stderr="Could not find service")
    again = li.uninstall(platform="darwin", runner=nothing_loaded, env=_mac_env(tmp_path), home=home)
    assert again.changed is False and again.message == "Start at login was already off."
    assert [c["argv"] for c in nothing_loaded.calls] == [
        ["/bin/launchctl", "bootout", f"gui/{li._uid()}/{LABEL}"]]

    original = _foreign_plist(result.path)
    runner = FakeRunner()
    with pytest.raises(ForeignLoginItem):
        li.uninstall(platform="darwin", runner=runner, env=_mac_env(tmp_path), home=home)
    assert result.path.read_bytes() == original
    assert runner.calls == []


def test_uninstall_unloads_a_stale_job_when_the_file_is_gone(tmp_path):
    runner = FakeRunner()          # bootout succeeds: a stale job was loaded
    result = li.uninstall(platform="darwin", runner=runner, env=_mac_env(tmp_path),
                          home=tmp_path / "home")
    assert result.changed is True and result.message == "Start at login is off."
    assert [c["argv"][1:] for c in runner.calls] == [["bootout", f"gui/{li._uid()}/{LABEL}"]]


def test_a_folder_at_the_plist_path_is_a_clean_failure(tmp_path):
    path = li.login_item_path("darwin", home=tmp_path / "home")
    path.mkdir(parents=True)
    runner = FakeRunner()
    for action in (li.install, li.uninstall):
        with pytest.raises(li.LoginItemFailed):
            action(platform="darwin", runner=runner, env=_mac_env(tmp_path), home=tmp_path / "home")
    assert path.is_dir() and runner.calls == []
    assert li.status(platform="darwin", runner=runner, env=_mac_env(tmp_path),
                     home=tmp_path / "home")["ours"] is False


def test_status_reports_installed_and_ours(tmp_path):
    home = tmp_path / "home"
    path = li.login_item_path("darwin", home=home)
    kw = dict(platform="darwin", runner=FakeRunner(), env=_mac_env(tmp_path), home=home)
    assert li.status(**kw) == {"installed": False, "ours": False, "path": str(path)}
    _mac_install(tmp_path)
    assert li.status(**kw) == {"installed": True, "ours": True, "path": str(path)}
    _foreign_plist(path)
    assert li.status(**kw) == {"installed": True, "ours": False, "path": str(path)}


# ── Windows (no Windows machine: platform="win32", fake env and runner) ──────

def _win_env(tmp_path, **extra):
    return {"APPDATA": str(tmp_path / "AppData" / "Roaming"), "PATH": str(tmp_path / "nobin"),
            "SystemRoot": r"C:\Windows", **extra}


class FakeShell:
    """A fake PowerShell: CreateShortcut writes a JSON stand-in for the .lnk,
    the read script prints it back. Paths arrive only through OL_* env vars."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        env = dict(kwargs.get("env") or {})
        self.calls.append({"argv": list(argv), "env": env})
        script = argv[-1]
        lnk = Path(env["OL_LNK"])
        if ".Save()" in script:
            lnk.parent.mkdir(parents=True, exist_ok=True)
            lnk.write_text(json.dumps({"Description": env["OL_DESC"], "TargetPath": env["OL_TARGET"],
                                       "Arguments": env["OL_ARGS"]}), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, lnk.read_text(encoding="utf-8"), "")


def test_windows_path_from_appdata(tmp_path):
    env = _win_env(tmp_path)
    path = li.login_item_path("win32", env=env, home=tmp_path / "home")
    assert path == (Path(env["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                    / "Startup" / WINDOWS_SHORTCUT)


def test_windows_script_never_interpolates_paths(tmp_path):
    evil = tmp_path / "x'; Remove-Item -Recurse $HOME ;'"
    env = _win_env(tmp_path, APPDATA=str(evil))
    shell = FakeShell()
    result = li.install(platform="win32", runner=shell, env=env, home=tmp_path / "home")
    assert result.changed is True
    create = shell.calls[-1]
    assert create["argv"][1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert create["argv"][0].lower().endswith("powershell.exe")
    script = create["argv"][-1]
    assert "Remove-Item" not in script and str(evil) not in script
    for name in ("OL_LNK", "OL_TARGET", "OL_ARGS", "OL_WORKDIR", "OL_DESC"):
        assert f"$env:{name}" in script
    assert create["env"]["OL_LNK"] == str(result.path)
    assert "Remove-Item" in create["env"]["OL_LNK"]
    assert create["env"]["OL_DESC"] == MARKER
    assert "WScript.Shell" in script and "CreateShortcut($env:OL_LNK)" in script


def test_windows_install_then_uninstall(tmp_path):
    env = _win_env(tmp_path)
    shell = FakeShell()
    first = li.install(platform="win32", runner=shell, env=env, home=tmp_path / "home", port=5596)
    assert first.changed is True and first.path.exists()
    assert li.read_shortcut(first.path, shell)["Arguments"].endswith("tray --port 5596")
    again = li.install(platform="win32", runner=shell, env=env, home=tmp_path / "home", port=5596)
    assert again.changed is False and again.message == "Already starts at login."
    assert li.status(platform="win32", runner=shell, env=env, home=tmp_path / "home") == {
        "installed": True, "ours": True, "path": str(first.path)}
    gone = li.uninstall(platform="win32", runner=shell, env=env, home=tmp_path / "home")
    assert gone.changed is True and not first.path.exists()


def test_windows_foreign_shortcut_is_refused(tmp_path):
    env = _win_env(tmp_path)
    path = li.login_item_path("win32", env=env)
    path.parent.mkdir(parents=True)
    original = json.dumps({"Description": "Someone else", "TargetPath": "x", "Arguments": ""})
    path.write_text(original, encoding="utf-8")
    shell = FakeShell()
    for action in (li.install, li.uninstall):
        with pytest.raises(ForeignLoginItem):
            action(platform="win32", runner=shell, env=env, home=tmp_path / "home")
    assert path.read_text(encoding="utf-8") == original
    assert all(".Save()" not in c["argv"][-1] for c in shell.calls)


def test_windows_refuses_a_custom_home_the_shortcut_cannot_carry(tmp_path):
    # F-32: a .lnk has no environment; a custom SELLBIRD_HOME set only in this
    # shell would make the login-started tray use a different home.
    env = _win_env(tmp_path, SELLBIRD_HOME=str(tmp_path / "custom"))
    shell = FakeShell()
    with pytest.raises(UnsupportedPlatform) as err:
        li.install(platform="win32", runner=shell, env=env, home=tmp_path / "home",
                   user_env=lambda name: None)
    assert str(err.value) == (
        "Start at login on Windows uses the default home folder. Set SELLBIRD_HOME as a user "
        "environment variable (System Properties → Environment Variables), then run this again.")
    assert shell.calls == []

    # Set in the user registry too: every login process inherits it, so it is fine.
    ok = li.install(platform="win32", runner=shell, env=env, home=tmp_path / "home",
                    user_env=lambda name: env["SELLBIRD_HOME"] if name == "SELLBIRD_HOME" else None)
    assert ok.changed is True

    # The default home spelled out explicitly is also fine.
    default = str(Path(env["APPDATA"]) / "sellbird")
    li.uninstall(platform="win32", runner=shell, env=env, home=tmp_path / "home")
    ok = li.install(platform="win32", runner=shell, env={**env, "SELLBIRD_HOME": default},
                    home=tmp_path / "home", user_env=lambda name: None)
    assert ok.changed is True


def test_windows_without_appdata_is_a_clean_failure(tmp_path, capsys):
    env = {"PATH": str(tmp_path)}
    with pytest.raises(li.LoginItemFailed) as err:
        li.install(platform="win32", runner=FakeShell(), env=env, home=tmp_path)
    assert "APPDATA" in str(err.value)
    from sellbird.desktop_cli import cmd_autostart
    with pytest.raises(SystemExit) as exited:
        cmd_autostart(argparse.Namespace(autostart_action="status", port=None), platform="win32",
                      runner=FakeShell(), env=env, home=tmp_path)
    assert exited.value.code == 1 and "APPDATA" in capsys.readouterr().err


def test_tray_argv_prefers_pythonw_on_windows(tmp_path, monkeypatch):
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert li.tray_argv("win32") == [str(tmp_path / "python.exe"), "-P", "-m", "sellbird", "tray"]
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    assert li.tray_argv("win32")[0] == str(tmp_path / "pythonw.exe")
    assert li.tray_argv("darwin", port=5596) == [str(tmp_path / "python.exe"), "-P", "-m",
                                                  "sellbird", "tray", "--port", "5596"]


def test_unsupported_platform_message(tmp_path):
    for action in (li.install, li.uninstall, li.status):
        with pytest.raises(UnsupportedPlatform) as err:
            action(platform="linux", runner=FakeRunner(), env={}, home=tmp_path)
        assert str(err.value) == "Start at login works on macOS and Windows."


@pytest.mark.skipif(sys.platform != "win32", reason="real shortcut needs Windows")
def test_real_shortcut_round_trip(tmp_path, monkeypatch):
    env = {**os.environ, "APPDATA": str(tmp_path / "AppData")}
    env.pop("SELLBIRD_HOME", None)
    result = li.install(platform="win32", env=env, home=tmp_path)
    assert li.read_shortcut(result.path, subprocess.run)["Description"] == MARKER
    li.uninstall(platform="win32", env=env, home=tmp_path)
    assert not result.path.exists()


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli(tmp_path, action, runner, port=None):
    from sellbird.desktop_cli import cmd_autostart
    args = argparse.Namespace(autostart_action=action, port=port)
    return cmd_autostart(args, platform="darwin", runner=runner, env=_mac_env(tmp_path),
                         home=tmp_path / "home")


def test_cli_install_status_uninstall(tmp_path, capsys):
    runner = FakeRunner()
    _cli(tmp_path, "install", runner, port=5596)
    assert "Sellbird starts at login, and is starting now." in capsys.readouterr().out
    _cli(tmp_path, "status", runner)
    out = capsys.readouterr().out
    assert "on" in out.lower() and f"{LABEL}.plist" in out
    _cli(tmp_path, "uninstall", runner)
    assert "Start at login is off." in capsys.readouterr().out


def test_cli_foreign_file_exits_1(tmp_path, capsys):
    path = li.login_item_path("darwin", home=tmp_path / "home")
    _foreign_plist(path)
    with pytest.raises(SystemExit) as err:
        _cli(tmp_path, "install", FakeRunner())
    assert err.value.code == 1
    assert "wasn't created by Sellbird. Nothing was changed." in capsys.readouterr().err


def test_cli_registers_autostart_parser():
    from sellbird.desktop_cli import register_desktop_commands
    parser = argparse.ArgumentParser()
    register_desktop_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["autostart", "install", "--port", "5596"])
    assert (args.autostart_action, args.port) == ("install", 5596)
    assert parser.parse_args(["autostart", "status"]).port is None
