"""`sellbird mcp install|uninstall [claude|codex]`: register the MCP server with
the user's coding agent.

These are the user's own config files, shared with everything else they run,
so the rules are strict:

- Show the exact change first, and apply it only on a yes (or `--yes`).
- Touch only our entry. A server named Sellbird that this command didn't
  write is refused and nothing changes. In Codex's TOML "ours" is proven by
  the marker comment just above our table.
- Files: back up, write a temp file, atomic replace, re-read and verify that
  only our entry moved. If it didn't check out, restore the original.
- Claude Code: the `claude` CLI does the writing when it exists (Claude Code
  rewrites ~/.claude.json while it runs). Detection always reads the file,
  because `claude mcp get` prints human text, not data.
- Codex is touched only by an explicit `sellbird mcp install codex`. Sellbird's
  own Codex brain keeps its private CODEX_HOME; nothing here touches it.

The entry holds no token: the MCP server reads the agent token from the home
folder (SELLBIRD_HOME) at runtime.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Mapping

from sellbird.paths import HOME_ENV, PROJECT_ROOT
from sellbird.workspace_home import NotOnHosted, require_self_hosted

SERVER_NAME = "sellbird"
CODEX_MARKER = ("# Added by Sellbird (sellbird mcp install). "
                "Remove with: sellbird mcp uninstall codex")

CLIENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}
RESTART_CLAUDE = "If Claude Code is open, restart it."

REFUSE_NO_CLAUDE = ("Claude Code isn't installed here (no `claude` command "
                    "and no ~/.claude.json).")
REFUSE_BAD_JSON = "~/.claude.json isn't valid JSON, so it was left alone."
REFUSE_FOREIGN_CLAUDE = ("~/.claude.json already has a Sellbird server that this "
                         "command didn't add. Edit it yourself, or remove it and run this again.")
REFUSE_BAD_TOML = "~/.codex/config.toml isn't valid TOML, so it was left alone."
REFUSE_FOREIGN_CODEX = ("~/.codex/config.toml already has a Sellbird server that this "
                        "command didn't add. Edit it yourself, or remove it and run this again.")
VERIFY_FAILED = "The change didn't check out, so your original file was restored."
NOT_TTY = "Nothing was changed. Re-run with --yes to apply."
CLAUDE_LABEL = "~/.claude.json"
CODEX_LABEL = "~/.codex/config.toml"


def _symlink_refusal(label: str) -> str:
    return (f"{label} is a symlink, so it was left alone. Add the entry to the file it "
            f"points to yourself (sellbird mcp install --print shows it).")


def _unreadable(label: str, err: OSError) -> str:
    return f"{label} couldn't be read ({err.strerror or type(err).__name__}), so it was left alone."


class InstallError(Exception):
    """The change couldn't be made. The message is safe to show."""


class InstallVerifyFailed(InstallError):
    """The written file didn't check out; the original was put back."""


@dataclass(frozen=True)
class PlannedChange:
    client: Literal["claude", "codex"]
    # add / replace / remove edit the file at `path`; cli runs `argv` (after
    # `pre_argv`, when an older entry of ours has to go first).
    action: Literal["add", "replace", "remove", "none", "refuse", "cli"]
    path: Path | None
    preview: str
    reason: str = ""
    argv: tuple[str, ...] = ()
    pre_argv: tuple[str, ...] = ()
    # The entry `pre_argv` removes, as JSON, so a failed add can put it back.
    old_json: str = ""


def server_entry() -> dict:
    # -P: the client's working folder never goes on sys.path, so a `sellbird/`
    # folder there (a Sellbird checkout) can't stand in for this package.
    return {"command": sys.executable, "args": ["-P", "-m", "sellbird", "mcp"],
            "env": {HOME_ENV: str(PROJECT_ROOT)}}


def is_ours(entry) -> bool:
    """Launched as `python [-P] -m sellbird mcp` or `sellbird mcp`: written by this command."""
    if not isinstance(entry, dict):
        return False
    args = entry.get("args") or []
    if not isinstance(args, list):
        return False
    if args[-3:] == ["-m", "sellbird", "mcp"]:
        return True
    command = str(entry.get("command") or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return command.removesuffix(".exe") == "sellbird" and args == ["mcp"]


def _same(entry) -> bool:
    """Equal to ours. Claude's CLI stores "type": "stdio", the default."""
    if not isinstance(entry, dict):
        return False
    if entry.get("type") == "stdio":
        entry = {k: v for k, v in entry.items() if k != "type"}
    return entry == server_entry()


def _prefixed(sign: str, text: str) -> list[str]:
    return [f"{sign} {line}" if line else sign for line in text.splitlines()]


# ── Claude Code ──

def _claude_path(home: Path) -> Path:
    return Path(home) / ".claude.json"


def _read_claude(path: Path) -> tuple[dict | None, str]:
    """(document, refusal). A missing file is an empty document."""
    if path.is_symlink():
        return None, _symlink_refusal(CLAUDE_LABEL)    # never replace a link with a file
    if not path.exists():
        return {}, ""
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except OSError as e:
        return None, _unreadable(CLAUDE_LABEL, e)
    except (ValueError, UnicodeDecodeError):
        return None, REFUSE_BAD_JSON
    if not isinstance(doc, dict) or not isinstance(doc.get("mcpServers", {}), dict):
        return None, REFUSE_BAD_JSON
    return doc, ""


def _claude_entry_preview(old, new) -> str:
    lines = []
    if old is not None:
        lines += _prefixed("-", json.dumps({SERVER_NAME: old}, indent=2))
    if new is not None:
        lines += _prefixed("+", json.dumps({SERVER_NAME: new}, indent=2))
    return "\n".join(lines)


def _claude_cli_argv(claude: str, verb: str, entry: dict | None = None) -> tuple[str, ...]:
    if verb == "add":
        return (claude, "mcp", "add-json", "--scope", "user", SERVER_NAME,
                json.dumps(server_entry() if entry is None else entry))
    return (claude, "mcp", "remove", SERVER_NAME, "--scope", "user")


def plan_claude(*, home: Path, which=shutil.which, runner=subprocess.run) -> PlannedChange:
    """What installing for Claude Code would do. Reads only; never runs `claude`."""
    path = _claude_path(home)
    claude = which("claude")
    if not claude and not path.exists():
        return PlannedChange("claude", "refuse", None, "", REFUSE_NO_CLAUDE)
    doc, refusal = _read_claude(path)
    if refusal:
        return PlannedChange("claude", "refuse", path, "", refusal)
    current = doc.get("mcpServers", {}).get(SERVER_NAME)
    if current is not None and _same(current):
        return PlannedChange("claude", "none", path, "")
    if current is not None and not is_ours(current):
        return PlannedChange("claude", "refuse", path, "", REFUSE_FOREIGN_CLAUDE)
    if claude:
        argv = _claude_cli_argv(claude, "add")
        pre = _claude_cli_argv(claude, "remove") if current is not None else ()
        preview = "\n".join([_claude_entry_preview(current, server_entry()), "Runs:"]
                            + [shlex.join(a) for a in (pre, argv) if a])
        return PlannedChange("claude", "cli", path, preview, argv=argv, pre_argv=pre,
                             old_json=json.dumps(current) if current is not None else "")
    action = "add" if current is None else "replace"
    return PlannedChange("claude", action, path,
                         _claude_entry_preview(current, server_entry()))


def plan_uninstall_claude(*, home: Path, which=shutil.which,
                          runner=subprocess.run) -> PlannedChange:
    path = _claude_path(home)
    claude = which("claude")
    if not claude and not path.exists():
        return PlannedChange("claude", "refuse", None, "", REFUSE_NO_CLAUDE)
    doc, refusal = _read_claude(path)
    if refusal:
        return PlannedChange("claude", "refuse", path, "", refusal)
    current = doc.get("mcpServers", {}).get(SERVER_NAME)
    if current is None:
        return PlannedChange("claude", "none", path, "",
                             "Sellbird isn't set up for Claude Code; nothing to remove.")
    if not is_ours(current):
        return PlannedChange("claude", "refuse", path, "", REFUSE_FOREIGN_CLAUDE)
    if claude:
        argv = _claude_cli_argv(claude, "remove")
        preview = "\n".join([_claude_entry_preview(current, None), "Runs:", shlex.join(argv)])
        return PlannedChange("claude", "cli", path, preview, argv=argv)
    return PlannedChange("claude", "remove", path, _claude_entry_preview(current, None))


def _claude_new_text(action: str, text: str) -> str:
    doc = json.loads(text) if text else {}
    servers = doc.setdefault("mcpServers", {})
    current = servers.get(SERVER_NAME)
    if current is not None and not is_ours(current):
        raise InstallError(REFUSE_FOREIGN_CLAUDE)      # appeared since the preview
    if action == "remove":
        servers.pop(SERVER_NAME, None)
    else:
        servers[SERVER_NAME] = server_entry()
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _claude_parts(text: str) -> tuple[dict, object]:
    """(document without our entry, our entry or None)."""
    doc = json.loads(text) if text else {}
    servers = dict(doc.get("mcpServers") or {})
    ours = servers.pop(SERVER_NAME, None)
    rest = {k: v for k, v in doc.items() if k != "mcpServers"}
    if servers:
        rest["mcpServers"] = servers
    return rest, ours


# ── Codex ──

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADER = re.compile(r"^\s*\[")
_OUR_HEADER = re.compile(
    r"""^\s*\[\s*mcp_servers\s*\.\s*(sellbird|"sellbird"|'sellbird')\s*\]\s*(#.*)?$""")


def _codex_path(env: Mapping[str, str], home: Path) -> Path:
    return Path(env.get("CODEX_HOME") or Path(home) / ".codex") / "config.toml"


def _toml_str(value: str) -> str:
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{escaped}"'


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_str(key)


def render_codex_block(entry: dict) -> str:
    args = ", ".join(_toml_str(a) for a in entry["args"])
    env = ", ".join(f"{_toml_key(k)} = {_toml_str(v)}" for k, v in entry["env"].items())
    return "\n".join([
        CODEX_MARKER,
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_toml_str(entry['command'])}",
        f"args = [{args}]",
        f"env = {{ {env} }}",
    ]) + "\n"


def _read_codex(path: Path) -> tuple[str | None, dict | None, str]:
    """(text, document, refusal). Text None: no file."""
    if path.is_symlink():
        return None, None, _symlink_refusal(CODEX_LABEL)
    if not path.exists():
        return None, {}, ""
    try:
        text = path.read_bytes().decode("utf-8")
        return text, tomllib.loads(text), ""
    except OSError as e:
        return None, None, _unreadable(CODEX_LABEL, e)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None, None, REFUSE_BAD_TOML


def _our_block(text: str) -> tuple[int, int] | None:
    """Line range [start, end) of our marked block, or None if not marked.

    It runs from the marker to the line before the next table header (or the
    end), less trailing blank and comment lines, which belong to what follows.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _OUR_HEADER.match(line):
            if i == 0 or lines[i - 1].rstrip("\r\n") != CODEX_MARKER:
                return None
            end = i + 1
            while end < len(lines) and not _HEADER.match(lines[end]):
                end += 1
            while end > i + 1 and (not lines[end - 1].strip()
                                   or lines[end - 1].lstrip().startswith("#")):
                end -= 1
            return i - 1, end
    return None


def plan_codex(*, env: Mapping[str, str], home: Path) -> PlannedChange:
    path = _codex_path(env, home)
    text, doc, refusal = _read_codex(path)
    if refusal:
        return PlannedChange("codex", "refuse", path, "", refusal)
    current = (doc.get("mcp_servers") or {}).get(SERVER_NAME)
    if current is not None and current == server_entry():
        return PlannedChange("codex", "none", path, "")
    new = _prefixed("+", render_codex_block(server_entry()))
    if current is None:
        return PlannedChange("codex", "add", path, "\n".join(new))
    block = _our_block(text or "")
    if block is None:
        return PlannedChange("codex", "refuse", path, "", REFUSE_FOREIGN_CODEX)
    old = "".join(text.splitlines(keepends=True)[block[0]:block[1]])
    return PlannedChange("codex", "replace", path, "\n".join(_prefixed("-", old) + new))


def plan_uninstall_codex(*, env: Mapping[str, str], home: Path) -> PlannedChange:
    path = _codex_path(env, home)
    text, doc, refusal = _read_codex(path)
    if refusal:
        return PlannedChange("codex", "refuse", path, "", refusal)
    if (doc.get("mcp_servers") or {}).get(SERVER_NAME) is None:
        return PlannedChange("codex", "none", path, "",
                             "Sellbird isn't set up for Codex; nothing to remove.")
    block = _our_block(text or "")
    if block is None:
        return PlannedChange("codex", "refuse", path, "", REFUSE_FOREIGN_CODEX)
    old = "".join(text.splitlines(keepends=True)[block[0]:block[1]])
    return PlannedChange("codex", "remove", path, "\n".join(_prefixed("-", old)))


def _codex_new_text(action: str, text: str) -> str:
    if action == "add":
        if text and not text.endswith("\n"):
            text += "\n"
        return text + ("\n" if text else "") + render_codex_block(server_entry())
    block = _our_block(text)
    if block is None:
        raise InstallError(REFUSE_FOREIGN_CODEX)       # changed since the preview
    lines = text.splitlines(keepends=True)
    start, end = block
    if action == "replace":
        return "".join(lines[:start]) + render_codex_block(server_entry()) + "".join(lines[end:])
    if start > 0 and not lines[start - 1].strip():
        start -= 1                                     # the blank line the add put there
    return "".join(lines[:start] + lines[end:])


def _codex_parts(text: str) -> tuple[dict, object]:
    doc = tomllib.loads(text) if text else {}
    servers = dict(doc.get("mcp_servers") or {})
    ours = servers.pop(SERVER_NAME, None)
    rest = {k: v for k, v in doc.items() if k != "mcp_servers"}
    if servers:
        rest["mcp_servers"] = servers
    return rest, ours


# ── Applying ──

def _backup_path(path: Path, now: datetime) -> Path:
    """A fresh name: two changes in one second must not overwrite a backup."""
    base = f"{path.name}.sellbird-backup-{now:%Y%m%d%H%M%S}"
    candidate, n = path.with_name(base), 2
    while candidate.exists():
        candidate, n = path.with_name(f"{base}-{n}"), n + 1
    return candidate


def _write_atomic(target: Path, text: str) -> None:
    """Temp file in the same folder, then os.replace: never a half-written file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        if target.exists():
            shutil.copymode(target, tmp)               # ~/.claude.json is private
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_private(path: Path, data: bytes, *, exclusive: bool) -> None:
    """Write `data` to a file created owner-only (0o600) whatever the umask, so
    a config's contents never sit in a world-readable file, even briefly."""
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _restore(data: bytes, backup: Path, target: Path) -> None:
    """Put the original bytes back, atomically, by its own path (not the writer)."""
    tmp = target.with_name(f".{target.name}.restore.tmp")
    try:
        tmp.unlink(missing_ok=True)                    # a leftover from a crash
        _write_private(tmp, data, exclusive=True)
        shutil.copymode(backup, tmp)                   # then the original's own mode
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _label(change: PlannedChange) -> str:
    return CLAUDE_LABEL if change.client == "claude" else CODEX_LABEL


def _cant_back_up(label: str, e: OSError) -> InstallError:
    return InstallError(f"{label} couldn't be read or backed up "
                        f"({e.strerror or type(e).__name__}), so nothing was changed.")


def _read(change: PlannedChange) -> bytes | None:
    """The file's bytes (None when it doesn't exist); a symlink is refused."""
    path, label = change.path, _label(change)
    if path.is_symlink():
        raise InstallError(_symlink_refusal(label))
    try:
        return path.read_bytes() if path.exists() else None
    except OSError as e:
        raise _cant_back_up(label, e)


def _back_up(change: PlannedChange, data: bytes | None, now: datetime) -> Path | None:
    """A backup of exactly these bytes, created owner-only, then given the
    original's mode (~/.claude.json is private)."""
    if data is None:
        return None
    path = change.path
    try:
        backup = _backup_path(path, now)
        _write_private(backup, data, exclusive=True)
        shutil.copymode(path, backup)
        return backup
    except OSError as e:
        raise _cant_back_up(_label(change), e)


def _read_and_back_up(change: PlannedChange, now: datetime) -> tuple[bytes | None, Path | None]:
    """The file's bytes and its backup, taken from those same bytes."""
    data = _read(change)
    return data, _back_up(change, data, now)


def _apply_file(change: PlannedChange, now: datetime) -> None:
    path = change.path
    data = _read(change)
    existed = data is not None
    try:
        original = data.decode("utf-8") if existed else ""
        if change.client == "claude":
            new_text, parts = _claude_new_text(change.action, original), _claude_parts
        else:
            new_text, parts = _codex_new_text(change.action, original), _codex_parts
    except (ValueError, UnicodeDecodeError):           # changed since the preview
        bad = REFUSE_BAD_JSON if change.client == "claude" else REFUSE_BAD_TOML
        raise InstallError(bad)
    backup = _back_up(change, data, now)               # only once it parsed: no stray backup
    try:
        _write_atomic(path, new_text)
        rest_after, ours_after = parts(path.read_bytes().decode("utf-8"))
        rest_before, _ = parts(original)
        wanted = None if change.action == "remove" else server_entry()
        ok = rest_after == rest_before and ours_after == wanted
    except Exception:
        ok = False
    if not ok:
        if existed:
            _restore(data, backup, path)
        else:
            path.unlink(missing_ok=True)
        raise InstallVerifyFailed(VERIFY_FAILED)


def _run_cli(argv: tuple[str, ...], runner) -> None:
    try:
        result = runner(list(argv), capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise InstallError(f"`{shlex.join(argv[:3])}` couldn't run ({type(e).__name__})")
    if result.returncode != 0:
        detail = ((result.stderr or result.stdout or "").strip().splitlines() or [""])[-1]
        raise InstallError(f"`{shlex.join(argv[:3])}` failed: {detail}".rstrip(": "))


def _kept_at(backup: Path | None) -> str:
    return f" Your previous file is at {backup}." if backup else ""


def _apply_cli(change: PlannedChange, runner, now: datetime, *, expect_absent: bool) -> None:
    """Run `claude mcp …`, roll back a half-done replace, then check the file."""
    _, backup = _read_and_back_up(change, now)
    if change.pre_argv:
        _run_cli(change.pre_argv, runner)
    try:
        _run_cli(change.argv, runner)
    except InstallError as add_failed:
        if not change.pre_argv:
            raise
        # The old entry is gone and the new one didn't go in: put the old one back.
        try:
            _run_cli(_claude_cli_argv(change.argv[0], "add", json.loads(change.old_json)),
                     runner)
        except InstallError:
            raise InstallError("The old Sellbird entry was removed and the new one "
                               f"couldn't be added ({add_failed})." + _kept_at(backup))
        raise InstallError(f"{add_failed}. Your previous Sellbird entry was put back.")
    doc, refusal = _read_claude(change.path)
    entry = (doc or {}).get("mcpServers", {}).get(SERVER_NAME)
    # An unreadable file afterwards proves nothing, even for a remove.
    if refusal or not (entry is None if expect_absent else _same(entry)):
        raise InstallError("Claude Code's config didn't end up as expected after "
                           "`claude mcp`." + _kept_at(backup))


def apply_change(change: PlannedChange, *, runner=subprocess.run, now: datetime) -> str:
    """Make the planned change and say what happened."""
    name = CLIENT_NAMES[change.client]
    if change.action == "refuse":
        return change.reason
    if change.action == "none":
        return change.reason or f"Already set up for {name}."
    removing = change.action == "remove" or (change.argv[2:3] == ("remove",))
    done = (f"Removed Sellbird from {name}." if removing
            else f"Added Sellbird to {name}.")
    if change.action == "cli":
        _apply_cli(change, runner, now, expect_absent=removing)
        return f"{done} Start a new Claude Code session to use it."
    _apply_file(change, now)
    if change.client == "claude":
        return f"{done} {RESTART_CLAUDE}"
    return f"{done} Restart Codex to pick it up."


def print_config() -> str:
    """Everything needed to add the server by hand. Writes nothing."""
    entry = server_entry()
    return "\n\n".join([
        json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2),
        render_codex_block(entry).rstrip("\n"),
        (f"Any other MCP client: command {entry['command']}, args -P -m sellbird mcp, "
         f"env {HOME_ENV}={entry['env'][HOME_ENV]}"),
    ])


# ── The command ──

def _codex_detected(which, env: Mapping[str, str], home: Path) -> bool:
    return bool(which("codex")) or _codex_path(env, home).parent.is_dir()


def run_command(action: str, client: str | None, *, print_only: bool = False,
                yes: bool = False, home: Path | None = None,
                env: Mapping[str, str] | None = None, which=shutil.which,
                runner=subprocess.run, isatty: Callable[[], bool] | None = None,
                ask: Callable[[str], str] = input, out: Callable[[str], None] = print,
                now: datetime | None = None) -> int:
    """`sellbird mcp install|uninstall [claude|codex]`. Returns the exit code.

    With no client only Claude Code is touched; Codex needs `codex` spelled
    out, and gets a one-line hint when it's installed.
    """
    try:
        require_self_hosted("mcp_install")  # a server has no coding agent to add
    except NotOnHosted as e:
        out(e.payload["message"])
        return 1
    if print_only:
        out(print_config())
        return 0
    home = Path.home() if home is None else Path(home)
    env = os.environ if env is None else env
    isatty = isatty or sys.stdin.isatty
    target = client or "claude"
    if target == "claude":
        plan = plan_claude if action == "install" else plan_uninstall_claude
        change = plan(home=home, which=which, runner=runner)
    else:
        plan = plan_codex if action == "install" else plan_uninstall_codex
        change = plan(env=env, home=home)

    if client is None and _codex_detected(which, env, home):
        verb = "add Sellbird to it too" if action == "install" else \
            "remove Sellbird from it too"
        hint = f"Codex found. To {verb}, run: sellbird mcp {action} codex"
    else:
        hint = ""

    code = _show_and_apply(change, yes=yes, isatty=isatty, ask=ask, out=out,
                           runner=runner, now=now or datetime.now())
    if hint:
        out(hint)
    return code


def _show_and_apply(change: PlannedChange, *, yes, isatty, ask, out, runner, now) -> int:
    name = CLIENT_NAMES[change.client]
    if change.action == "refuse":
        out(change.reason)
        return 1
    if change.action == "none":
        out(apply_change(change, runner=runner, now=now))
        return 0
    if change.action == "cli":
        out(f"{name}: this will run")
    else:
        out(f"{name}: {change.path}")
    out(change.preview)
    if not yes:
        if not isatty():
            out(NOT_TTY)
            return 2
        if ask("Apply this change? [y/N] ").strip().lower() not in ("y", "yes"):
            out("Nothing was changed.")
            return 1
    try:
        out(apply_change(change, runner=runner, now=now))
    except InstallError as e:
        out(str(e))
        return 1
    return 0
