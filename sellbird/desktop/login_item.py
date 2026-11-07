"""Start the tray at login: a LaunchAgent on macOS, a Startup shortcut on Windows.

Windows uses a Startup-folder shortcut rather than Task Scheduler: it is
per-user, needs no elevation, shows (and can be switched off) in Task Manager's
Startup apps, and is one file we can recognise by its Description. Logon tasks
are more often blocked by company policy and need edition-specific options.

We only ever touch our own entry. A plist counts as ours when it carries
PLIST_COMMENT *and* its Label is LABEL; a shortcut when its Description is
MARKER. Anything else at the same path is someone else's: we refuse and change
nothing. `launchctl bootout` only ever names our label.

The login item launches a command line (`python -P -m sellbird tray`), never tray
code, so this module has no tray imports. Everything that reaches the OS goes
through an injectable `runner`, `env` and `home`, so tests never touch the real
LaunchAgents folder, launchctl or the Startup folder.
"""

import json
import ntpath
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Mapping

from sellbird.paths import HOME_ENV, PROJECT_ROOT, user_home_default

LABEL = "com.sellbird.tray"
MARKER = "Created by Sellbird"
PLIST_COMMENT = "<!-- Created by Sellbird. Remove with: sellbird autostart uninstall -->"
WINDOWS_SHORTCUT = "Sellbird tray.lnk"

LAUNCHCTL = "/bin/launchctl"
# bootout returns before launchd finishes tearing the old job down, so an
# immediate bootstrap can fail with "Input/output error"; one retry after this.
BOOTSTRAP_RETRY_WAIT = 1.0


class UnsupportedPlatform(Exception):
    def __init__(self, message: str = "Start at login works on macOS and Windows."):
        super().__init__(message)


class ForeignLoginItem(Exception):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"{path} exists and wasn't created by Sellbird. Nothing was changed.")


class LoginItemFailed(Exception):
    """The OS refused to create the entry (e.g. PowerShell failed)."""


@dataclass(frozen=True)
class LoginItemResult:
    changed: bool
    path: Path
    message: str


def _platform(platform: str | None) -> str:
    return sys.platform if platform is None else platform


def login_item_path(platform: str | None = None, env: Mapping[str, str] | None = None,
                    home: Path | None = None) -> Path:
    platform = _platform(platform)
    env = os.environ if env is None else env
    if platform == "darwin":
        return Path(Path.home() if home is None else home) / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if platform == "win32":
        if not env.get("APPDATA"):
            raise LoginItemFailed("APPDATA isn't set, so the Windows Startup folder can't be found.")
        return (Path(env["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                / "Startup" / WINDOWS_SHORTCUT)
    raise UnsupportedPlatform()


def tray_argv(platform: str | None = None, port: int | None = None) -> list[str]:
    """The command the login item runs. On Windows, pythonw.exe so no console
    window opens at login. `--port` only when asked (F-31); otherwise the tray
    picks its own default."""
    python = sys.executable
    if _platform(platform) == "win32":
        pythonw = Path(python).with_name("pythonw.exe")
        if pythonw.exists():
            python = str(pythonw)
    argv = [python, "-P", "-m", "sellbird", "tray"]  # -P: see tray.core_command
    if port is not None:
        argv += ["--port", str(port)]
    return argv


# ── macOS ──────────────────────────────────────────────────────────────────

def render_plist(argv: list[str], path_env: str, home_env: str | None, log_path: Path,
                 working_dir: Path | None = None) -> bytes:
    env_vars = {"PATH": path_env}
    if home_env:
        env_vars[HOME_ENV] = home_env
    data = plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": list(argv),
        "RunAtLoad": True,
        # Relaunch only after a crash; a clean exit (quit from the menu) stays quit.
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "EnvironmentVariables": env_vars,
        # Same as the Windows shortcut: the home folder, not launchd's "/".
        "WorkingDirectory": str(PROJECT_ROOT if working_dir is None else working_dir),
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    })
    lines = data.split(b"\n")
    doctype = next(i for i, line in enumerate(lines) if line.startswith(b"<!DOCTYPE"))
    lines.insert(doctype + 1, PLIST_COMMENT.encode("utf-8"))
    return b"\n".join(lines)


def is_ours_plist(data: bytes) -> bool:
    if PLIST_COMMENT.encode("utf-8") not in data:
        return False
    try:
        parsed = plistlib.loads(data)
    except Exception:
        return False
    return isinstance(parsed, dict) and parsed.get("Label") == LABEL


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _uid() -> int:
    # Only the macOS branch needs it; getattr keeps platform="darwin" tests
    # importable and runnable on Windows, which has no os.getuid.
    return getattr(os, "getuid", lambda: 0)()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise LoginItemFailed(f"Couldn't read {path} ({e.strerror or type(e).__name__}). "
                              "Nothing was changed.") from None


def _launchctl(runner, *args: str):
    return runner([LAUNCHCTL, *args], capture_output=True, text=True)


def _install_mac(path: Path, runner, env: Mapping[str, str], port: int | None,
                 sleep) -> LoginItemResult:
    home_env = (env.get(HOME_ENV) or "").strip()
    home_env = str(Path(home_env).expanduser().resolve()) if home_env else None
    log_path = PROJECT_ROOT / "data" / "tray.log"
    # The install-time PATH: launchd's own PATH lacks Homebrew, ~/.local/bin, etc.
    data = render_plist(tray_argv("darwin", port), env.get("PATH", os.defpath), home_env, log_path)
    if path.exists():
        existing = _read_bytes(path)
        if not is_ours_plist(existing):
            raise ForeignLoginItem(path)
        if existing == data:
            return LoginItemResult(False, path, "Already starts at login.")
    # launchd won't create the log's folder.
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, data)
    except OSError as e:
        raise LoginItemFailed(f"Couldn't write {path} ({e.strerror or type(e).__name__}).") from None
    domain = f"gui/{_uid()}"
    _launchctl(runner, "bootout", f"{domain}/{LABEL}")  # not loaded yet is fine
    boot = _launchctl(runner, "bootstrap", domain, str(path))
    if boot.returncode != 0:
        sleep(BOOTSTRAP_RETRY_WAIT)
        boot = _launchctl(runner, "bootstrap", domain, str(path))
    if boot.returncode != 0:
        reason = (boot.stderr or "").strip() or f"exit {boot.returncode}"
        return LoginItemResult(True, path, f"Saved, but macOS didn't load it ({reason}). "
                                           "It will start at your next login.")
    return LoginItemResult(True, path, "Sellbird starts at login, and is starting now.")


def _uninstall_mac(path: Path, runner) -> LoginItemResult:
    if not path.exists():
        # The file is gone but a stale job may still be loaded: unload our
        # label (and only ours). Nothing loaded -> it really was already off.
        gone = _launchctl(runner, "bootout", f"gui/{_uid()}/{LABEL}")
        if gone.returncode == 0:
            return LoginItemResult(True, path, "Start at login is off.")
        return LoginItemResult(False, path, "Start at login was already off.")
    if not is_ours_plist(_read_bytes(path)):
        raise ForeignLoginItem(path)
    _launchctl(runner, "bootout", f"gui/{_uid()}/{LABEL}")
    try:
        path.unlink()
    except OSError as e:
        raise LoginItemFailed(f"Couldn't remove {path} ({e.strerror or type(e).__name__}).") from None
    return LoginItemResult(True, path, "Start at login is off.")


# ── Windows ────────────────────────────────────────────────────────────────

# Values arrive only through OL_* environment variables, never interpolated
# into the script text, so no path can inject PowerShell.
_READ_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:OL_LNK); "
    "@{Description = $s.Description; TargetPath = $s.TargetPath; Arguments = $s.Arguments}"
    " | ConvertTo-Json -Compress"
)
_CREATE_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:OL_LNK); "
    "$s.TargetPath = $env:OL_TARGET; $s.Arguments = $env:OL_ARGS; "
    "$s.WorkingDirectory = $env:OL_WORKDIR; $s.Description = $env:OL_DESC; $s.Save()"
)


def _powershell(env: Mapping[str, str]) -> str:
    found = shutil.which("powershell", path=env.get("PATH"))
    if found:
        return found
    root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
    return str(PureWindowsPath(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))


def _run_powershell(runner, script: str, env: Mapping[str, str], extra: Mapping[str, str]):
    return runner([_powershell(env), "-NoProfile", "-NonInteractive", "-Command", script],
                  env={**env, **extra}, capture_output=True, text=True)


def read_shortcut(path: Path, runner=subprocess.run,
                  env: Mapping[str, str] | None = None) -> dict | None:
    """{"Description", "TargetPath", "Arguments"}, or None when there's no file.
    An unreadable shortcut reads as blank, i.e. not ours."""
    if not Path(path).exists():
        return None
    env = os.environ if env is None else env
    blank = {"Description": "", "TargetPath": "", "Arguments": ""}
    proc = _run_powershell(runner, _READ_SCRIPT, env, {"OL_LNK": str(path)})
    if proc.returncode != 0:
        return blank
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return blank
    if not isinstance(data, dict):
        return blank
    return {key: str(data.get(key) or "") for key in blank}


def _same_windows_path(a: str, b: str) -> bool:
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def _user_registry_env(name: str) -> str | None:
    """A user environment variable from HKCU\\Environment (what every process
    started at login inherits), or None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except Exception:
        return None


def _check_windows_home(env: Mapping[str, str], home: Path | None,
                        user_env: Callable[[str], str | None]) -> None:
    """F-32: a .lnk can't carry environment variables, so a custom home set
    only in this shell would make the login-started tray use another home."""
    override = (env.get(HOME_ENV) or "").strip()
    if not override:
        return
    default = user_home_default("win32", env, Path.home() if home is None else home)
    if _same_windows_path(override, str(default)):
        return
    registry = user_env(HOME_ENV)
    if registry and _same_windows_path(registry.strip(), override):
        return
    raise UnsupportedPlatform(
        "Start at login on Windows uses the default home folder. Set SELLBIRD_HOME as a user "
        "environment variable (System Properties → Environment Variables), then run this again.")


def _install_windows(path: Path, runner, env: Mapping[str, str], home: Path | None,
                     port: int | None, user_env) -> LoginItemResult:
    _check_windows_home(env, home, user_env)
    argv = tray_argv("win32", port)
    target, arguments = argv[0], subprocess.list2cmdline(argv[1:])
    existing = read_shortcut(path, runner, env)
    if existing is not None:
        if existing["Description"] != MARKER:
            raise ForeignLoginItem(path)
        if existing["TargetPath"] == target and existing["Arguments"] == arguments:
            return LoginItemResult(False, path, "Already starts at login.")
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = _run_powershell(runner, _CREATE_SCRIPT, env, {
        "OL_LNK": str(path), "OL_TARGET": target, "OL_ARGS": arguments,
        "OL_WORKDIR": str(PROJECT_ROOT), "OL_DESC": MARKER,
    })
    if proc.returncode != 0:
        reason = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        raise LoginItemFailed(f"Windows couldn't create the Startup shortcut ({reason}).")
    return LoginItemResult(True, path, "Sellbird starts at login.")


def _uninstall_windows(path: Path, runner, env: Mapping[str, str]) -> LoginItemResult:
    existing = read_shortcut(path, runner, env)
    if existing is None:
        return LoginItemResult(False, path, "Start at login was already off.")
    if existing["Description"] != MARKER:
        raise ForeignLoginItem(path)
    try:
        path.unlink()
    except OSError as e:
        raise LoginItemFailed(f"Couldn't remove {path} ({e.strerror or type(e).__name__}).") from None
    return LoginItemResult(True, path, "Start at login is off.")


# ── Public entry points ───────────────────────────────────────────────────

def install(*, platform: str | None = None, runner=subprocess.run,
            env: Mapping[str, str] | None = None, home: Path | None = None,
            port: int | None = None,
            user_env: Callable[[str], str | None] = _user_registry_env,
            sleep: Callable[[float], None] = time.sleep) -> LoginItemResult:
    platform = _platform(platform)
    env = os.environ if env is None else env
    path = login_item_path(platform, env, home)
    if platform == "darwin":
        return _install_mac(path, runner, env, port, sleep)
    return _install_windows(path, runner, env, home, port, user_env)


def uninstall(*, platform: str | None = None, runner=subprocess.run,
              env: Mapping[str, str] | None = None, home: Path | None = None) -> LoginItemResult:
    platform = _platform(platform)
    env = os.environ if env is None else env
    path = login_item_path(platform, env, home)
    if platform == "darwin":
        return _uninstall_mac(path, runner)
    return _uninstall_windows(path, runner, env)


def status(*, platform: str | None = None, runner=subprocess.run,
           env: Mapping[str, str] | None = None, home: Path | None = None) -> dict:
    platform = _platform(platform)
    env = os.environ if env is None else env
    path = login_item_path(platform, env, home)
    if platform == "darwin":
        installed = path.exists()
        try:
            ours = installed and is_ours_plist(path.read_bytes())
        except OSError:  # e.g. a folder at that path: there, but not ours
            ours = False
    else:
        existing = read_shortcut(path, runner, env)
        installed = existing is not None
        ours = installed and existing["Description"] == MARKER
    return {"installed": installed, "ours": ours, "path": str(path)}
