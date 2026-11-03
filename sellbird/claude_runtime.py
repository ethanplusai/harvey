"""How Harvey starts the Claude CLI: no tools, and none of the operator's own
Claude Code customisation.

Harvey's prompts carry untrusted text and its output goes to third parties
as email, so a brain call must see only what Harvey sends it. A plain
``claude -p`` loads the operator's ``~/.claude/CLAUDE.md``, any ``CLAUDE.md``
in the working directory or its ancestors, settings hooks, skills, plugins
and MCP servers, and saves a transcript of the prompt under
``~/.claude/projects``. Every call here instead runs with:

- ``--safe-mode``: CLAUDE.md, skills, plugins, hooks, MCP servers, custom
  commands and agents disabled. Login (OAuth, keychain or token) still works
  and admin-managed policy still applies. ``--bare`` is NOT used: it never
  reads OAuth or the keychain, which would break subscription login.
- ``--setting-sources ""``: no user, project or local settings files.
- ``--tools ""`` and ``--strict-mcp-config``: no built-in tools, no MCP.
- ``--disable-slash-commands`` and ``--no-session-persistence``: no skills,
  and nothing about the call is written to the operator's transcripts.
- a fresh, empty temporary working directory (never the checkout, whose own
  CLAUDE.md is the setup wizard), with ``PWD`` pointed at it;
- the prompt on stdin, never in argv, where other local users can read it
  (``ps``) and where Windows ``.cmd`` shims would re-parse it;
- a few inherited variables that load extra instructions or tie the call to
  an enclosing Claude Code session removed. Login, proxy and provider
  variables are left exactly as they are.

Before any prompt is sent the CLI's ``--help`` must list every one of those
flags; if any is missing Harvey refuses to call it (there is no fallback to
an unisolated call). That check proves the flags exist, not what the CLI
does with them: the behaviour itself is verified offline against the
installed CLI by ``tests/test_claude_cli_isolation.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

REQUIRED_FLAGS = (
    "--safe-mode",
    "--setting-sources",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--tools",
    "--output-format",
)

# Inherited variables that add instruction sources or bind the call to an
# enclosing Claude Code session (e.g. Harvey started from inside Claude Code).
# CLAUDE_CODE_SIMPLE is --bare mode, which disables subscription login.
STRIPPED_ENV = (
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    "CLAUDE_CODE_SIMPLE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_ENV_FILE",
    "CLAUDE_PROJECT_DIR",
    "CLAUDECODE",
    "OLDPWD",
)

HELP_TIMEOUT_SECONDS = 30

_capable: dict[tuple, bool] = {}
_capable_lock = threading.Lock()


class ClaudeRuntimeError(Exception):
    """The Claude CLI can't be used safely until the operator acts."""


_PLATFORM = sys.platform


# npm/yarn/pnpm shims name their target relative to the shim's folder
#   "%dp0%\node_modules\@anthropic-ai\claude-code\bin\claude.exe"   %*
# or, on another drive, by absolute path. Only claude.exe counts: older
# shims also name node.exe.
_SHIM_TARGET_RE = re.compile(
    r'"(?:%~?dp0%?\\)?((?:[A-Za-z]:\\)?[^"%]*?\bclaude\.exe)"', re.IGNORECASE)
# Before its install script runs, the package's bin/claude.exe is a small
# text placeholder, not the real binary.
_MIN_NATIVE_BYTES = 4096
INSTALL_HINT = (
    "Install Claude Code (https://code.claude.com/docs/en/setup): on Windows run "
    "'irm https://claude.ai/install.ps1 | iex' in PowerShell or "
    "'winget install Anthropic.ClaudeCode'; or 'npm install -g "
    "@anthropic-ai/claude-code@latest' (without --ignore-scripts)."
)


def _shim_target(shim: Path) -> Path:
    """The native exe a .cmd shim launches (read from the shim, else npm's
    usual layout next to it)."""
    try:
        m = _SHIM_TARGET_RE.search(shim.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        m = None
    if m:
        target = m.group(1).replace("\\", "/")
        return Path(target) if re.match(r"[A-Za-z]:/", target) else shim.parent / target
    return shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"


def _locate(env: dict | None) -> tuple[str | None, Path | None]:
    """(what PATH finds, the native exe a Windows shim wraps or None)."""
    path = (env if env is not None else os.environ).get("PATH")
    found = shutil.which("claude", path=path)
    if not found or _PLATFORM != "win32" or not found.lower().endswith((".cmd", ".bat")):
        return found, None
    return found, _shim_target(Path(found))


def find_claude(env: dict | None = None) -> str | None:
    """Full path of the ``claude`` executable on the PATH Harvey will use.

    On Windows an npm install puts a ``claude.cmd`` shim on PATH. Running it
    goes through cmd.exe, which re-parses every argument, and the isolation
    flags include empty ones (``--tools ""``) whose meaning changes if one is
    dropped. So the shim is resolved to the native ``claude.exe`` it wraps,
    and refused if that isn't there.
    """
    found, native = _locate(env)
    if native is None:
        return found
    try:
        size = native.stat().st_size if native.is_file() else None
    except OSError:
        size = None  # removed between the checks: treated as missing
    if size is not None and size >= _MIN_NATIVE_BYTES:
        return str(native)
    problem = ("is only a placeholder: the package's install script didn't run"
               if size is not None else "isn't there")
    raise ClaudeRuntimeError(
        f"Found the claude.cmd shim ({found}), but the claude.exe it launches "
        f"({native}) {problem}. Harvey never runs Claude through cmd.exe, which "
        f"re-parses the flags that keep each call isolated. {INSTALL_HINT}")


def runtime_env(cwd: str, base: dict | None = None) -> dict:
    """The child's environment: the operator's, minus instruction sources."""
    env = dict(os.environ if base is None else base)
    for key in STRIPPED_ENV:
        env.pop(key, None)
    env["PWD"] = cwd
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    return env


def build_command(executable: str, model: str = "", session_id: str | None = None) -> list[str]:
    """argv for one isolated, tool-less, print-mode call. The prompt goes
    on stdin."""
    cmd = [
        executable, "-p",
        # JSON output carries exact token/cost accounting per call.
        "--output-format", "json",
        # No tools at all: prompts carry untrusted text (prospect replies,
        # scraped pages); with no tools an injected instruction has nothing
        # to act with, and --strict-mcp-config keeps the operator's MCP
        # servers and connectors out.
        "--tools", "",
        "--strict-mcp-config",
        # None of the operator's CLAUDE.md, hooks, skills, plugins or
        # settings files, and no transcript left behind.
        "--safe-mode",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if model:
        # Validated by BrainConfig; affects this call only.
        cmd += ["--model", model]
    if session_id:
        # The CLI only accepts UUIDs; Harvey's friendly labels are dropped.
        try:
            uuid.UUID(session_id)
            cmd += ["--session-id", session_id]
        except ValueError:
            pass
    return cmd


@contextmanager
def isolated_cwd():
    """A fresh empty directory outside the checkout, removed afterwards. A
    failed removal (Windows, a child still exiting) never masks the call's
    own outcome or a cancellation."""
    with tempfile.TemporaryDirectory(prefix="harvey-claude-",
                                     ignore_cleanup_errors=True) as tmp:
        yield str(Path(tmp).resolve())


def _fingerprint(executable: str) -> tuple:
    real = os.path.realpath(executable)
    try:
        st = os.stat(real)
        return (real, st.st_mtime_ns, st.st_size)
    except OSError:
        return (real, None, None)


def fingerprint(env: dict | None = None) -> tuple:
    """Changes when the installed CLI changes (used to re-check a pause)."""
    found, native = _locate(env)
    # A Windows shim is tracked by the exe it wraps, so the fingerprint
    # changes when that's installed or updated.
    exe = str(native) if native is not None else found
    return _fingerprint(exe) if exe else ("", None, None)


def _probe_help(executable: str) -> str:
    """``claude --help`` output (no login, no model call). A nonzero exit
    raises CalledProcessError, whatever the output says: a CLI that fails
    its own help isn't trusted to honour the flags it lists."""
    with isolated_cwd() as cwd:
        out = subprocess.run(
            [executable, "--help"],
            cwd=cwd, env=runtime_env(cwd), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=HELP_TIMEOUT_SECONDS,
        )
    out.check_returncode()
    return (out.stdout or "") + (out.stderr or "")


def missing_flags(help_text: str) -> list[str]:
    return [f for f in REQUIRED_FLAGS
            if not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", help_text)]


def check_cli(env: dict | None = None) -> str:
    """Path of a Claude CLI that supports every isolation flag, or raise
    ClaudeRuntimeError saying what to do. A passing result is cached until
    the executable changes."""
    exe = find_claude(env)
    if not exe:
        raise ClaudeRuntimeError(
            "Claude CLI not found on PATH. Install it (https://code.claude.com/docs/en/setup) "
            "and run 'claude login'.")
    key = _fingerprint(exe)
    with _capable_lock:
        if _capable.get(key):
            return exe
    try:
        help_text = _probe_help(exe)
    except (OSError, subprocess.SubprocessError) as e:
        raise ClaudeRuntimeError(
            f"Could not run '{exe} --help' to check the Claude CLI ({type(e).__name__}). "
            "Harvey sends no prompt until it can confirm the CLI isolates calls.") from e
    missing = missing_flags(help_text)
    if missing:
        raise ClaudeRuntimeError(
            f"This Claude CLI ({exe}) lacks {', '.join(missing)}, which Harvey needs "
            "to keep your CLAUDE.md, hooks, skills, plugins and settings out of its "
            "prompts. Update it ('claude update', or rebuild the Docker image) and "
            "Harvey resumes on its own. No prompt was sent.")
    with _capable_lock:
        _capable[key] = True
    return exe


def clear_cache():
    with _capable_lock:
        _capable.clear()
