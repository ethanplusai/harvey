"""How Sellbird starts the Kimi Code CLI: no tools, and none of the
operator's own Kimi customisation.

A third brain provider, alongside claude_runtime.py and codex.py, for
operators whose subscription is a Kimi membership. Each call runs
``kimi -p --output-format stream-json`` with:

- ``--agent-file`` pointing at a Sellbird-shipped agent whose frontmatter
  declares ``tools: []``: the model can only answer with text, so a prompt
  carrying untrusted text (prospect replies, scraped pages) gives an
  injected instruction nothing to act with. Tool access is enforced again
  at execution time by the CLI, not just hidden from the prompt.
- ``KIMI_CODE_HOME`` pointed at a Sellbird-owned home folder (next to the
  database): the operator's own ``~/.kimi-code`` config, skills, plugins,
  global AGENTS.md, MCP servers and session transcripts are never read or
  written. Only ``credentials/`` (the OAuth login) is symlinked in, so
  ``kimi login`` in the operator's own terminal is the single sign-in.
- the operator's ``config.toml`` copied into that home (never symlinked:
  a future Sellbird deny rule must not edit the operator's own config),
  so the model aliases and providers the operator already has keep working;
- a fresh, empty temporary working directory (never the checkout), with
  ``PWD`` pointed at it.

Differences from the Claude runtime, by design of the Kimi CLI:

- The prompt travels on argv (``-p <prompt>``): the Kimi CLI has no
  stdin-prompt mode. On a shared machine the prompt is visible in ``ps``
  while the call runs, the way any long command line is; nothing else on
  the machine may read it, and the call itself is seconds-to-minutes.
  Sellbird documents this rather than piping through a shell.
- There is no ``--tools``/``--safe-mode`` flag family: the agent file is
  the documented tool-off switch, which is why it is required, not
  optional, and why ``--agent-file`` is in REQUIRED_FLAGS.
- Usage/cost per call is not reported by the CLI's stream-json, so no
  usage row is written; Sellbird's own call counting still applies.

Before any prompt is sent the CLI's ``--help`` must list every required
flag; if any is missing Sellbird refuses to call it (no fallback to an
unisolated call), same contract as claude_runtime.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

REQUIRED_FLAGS = (
    "--prompt",
    "--output-format",
    "--agent-file",
)

HELP_TIMEOUT_SECONDS = 30

_capable: dict[tuple, bool] = {}
_capable_lock = threading.Lock()

INSTALL_HINT = (
    "Install Kimi Code (https://www.kimi.com/code/docs/en/kimi-code-cli/guides/"
    "getting-started.html) and run 'kimi login'."
)

# The text-only agent every brain call runs under. tools: [] is the Kimi
# CLI's documented way to disable all tool access for the session.
NO_TOOLS_AGENT = """\
---
name: sellbird-brain
description: Text-only brain for Sellbird outreach calls
tools: []
---
You are the thinking engine behind a sales outreach tool. You transform the
operator's request into exactly the output it asks for. You have no tools:
never attempt tool calls, never inspect files, never run commands, never
ask questions. Reply with only the requested content.
"""


class KimiRuntimeError(Exception):
    """The Kimi CLI can't be used safely until the operator acts."""


def find_kimi(env: dict | None = None) -> str | None:
    """Full path of the ``kimi`` executable on the PATH Sellbird will use."""
    path = (env if env is not None else os.environ).get("PATH")
    return shutil.which("kimi", path=path)


def kimi_home() -> Path:
    """Sellbird's isolated KIMI_CODE_HOME (created on first use).

    Lives inside the current Sellbird home's data dir so it moves with
    SELLBIRD_HOME / the checkout like everything else Sellbird owns.
    """
    from sellbird.workspace_home import current_home

    home = current_home()
    return Path(home.data_dir) / "kimi-home"


def _operator_home() -> Path:
    return Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code"))


def _link_credentials(target: Path) -> None:
    """The OAuth login is the operator's, in their real home: reuse it via
    symlink so 'kimi login' stays the single sign-in. Windows without
    developer mode cannot create symlinks: fall back to a copy, accepting
    that a later 'kimi login' is not seen here until this folder is
    refreshed (delete it and let ensure_home copy again)."""
    real = _operator_home() / "credentials"
    link = target / "credentials"
    if link.is_symlink():
        if link.resolve() == real.resolve():
            return
        link.unlink()
    elif link.exists():
        return  # a real dir the CLI created: do not touch it
    if not real.is_dir():
        return
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        shutil.copytree(real, link)


def _copy_config(target: Path) -> None:
    """The operator's config (providers, model aliases) copied into the
    isolated home, so brain.model can name any alias they have. Copied, not
    symlinked: Sellbird never edits the operator's own config."""
    dest = target / "config.toml"
    if dest.exists():
        return
    src = _operator_home() / "config.toml"
    if src.is_file():
        shutil.copyfile(src, dest)
    else:
        # No operator config at all: seed the managed provider so a fresh
        # 'kimi login' works out of the box.
        dest.write_text(
            'default_model = "kimi-code/kimi-for-coding"\n\n'
            '[providers."managed:kimi-code"]\n'
            'type = "kimi"\n'
            'base_url = "https://api.kimi.com/coding/v1"\n'
            'api_key = ""\n\n'
            '[models."kimi-code/kimi-for-coding"]\n'
            'provider = "managed:kimi-code"\n'
            'model = "kimi-for-coding"\n',
            encoding="utf-8",
        )
    dest.chmod(stat.S_IRUSR | stat.S_IWUSR)


def ensure_home() -> Path:
    """Create the isolated home on first use; upgrade it in place after."""
    target = kimi_home()
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(stat.S_IRWXU)
    _link_credentials(target)
    _copy_config(target)
    agent = target / "sellbird-brain.md"
    if not agent.exists() or agent.read_text(encoding="utf-8") != NO_TOOLS_AGENT:
        agent.write_text(NO_TOOLS_AGENT, encoding="utf-8")
        agent.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return target


def runtime_env(cwd: str, base: dict | None = None) -> dict:
    """The child's environment: the operator's, relocated to Sellbird's
    own KIMI_CODE_HOME, with PWD on the isolated cwd. Login, proxy and
    provider variables are left exactly as they are (the credentials the
    login needs are reached through the symlinked dir)."""
    env = dict(os.environ if base is None else base)
    env["KIMI_CODE_HOME"] = str(ensure_home())
    env["PWD"] = cwd
    return env


def build_command(
    executable: str, prompt: str, model: str = "", session_id: str | None = None
) -> list[str]:
    """argv for one isolated, tool-less, print-mode call. The prompt goes
    on argv: the Kimi CLI's -p takes its value there (its --help lists no
    stdin-prompt mode), which is why --prompt is a required flag."""
    cmd = [
        executable,
        "-p", prompt,
        "--output-format", "stream-json",
        "--agent-file", str(kimi_home() / "sellbird-brain.md"),
    ]
    if model:
        # Validated by BrainConfig; must name an alias defined in the
        # isolated home's config.toml (the operator's copy, by default).
        cmd += ["--model", model]
    # session_id is a Sellbird label for the usage ledger; the Kimi CLI
    # binds no --session-id, and each call is its own throwaway session.
    return cmd


@contextmanager
def isolated_cwd():
    """A fresh empty directory outside the checkout, removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="sellbird-kimi-",
                                     ignore_cleanup_errors=True) as tmp:
        yield str(Path(tmp).resolve())


def parse_stream_json(raw: str) -> tuple[str, dict | None]:
    """stream-json stdout -> (response_text, payload).

    Each stdout line is one JSON object; assistant messages contribute
    their text in order. Anything that is not an assistant/meta line is
    ignored, so a formatting addition can never break the pipeline. The
    payload carries just enough for the ledger; the Kimi CLI reports no
    per-call token usage, so no usage rows are written from it.
    """
    parts: list[str] = []
    is_error = False
    session_id = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        role = obj.get("role")
        if role == "assistant":
            content = obj.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
        elif role == "meta":
            mtype = str(obj.get("type") or "")
            if mtype == "session.resume_hint" and not session_id:
                session_id = str(obj.get("session_id") or "")
            elif "error" in mtype.lower():
                is_error = True
    text = "".join(parts).strip()
    payload = {
        "is_error": is_error,
        "session_id": session_id,
        "duration_ms": 0,
        "num_turns": 1 if text else 0,
    }
    return text, payload


def _fingerprint(executable: str) -> tuple:
    real = os.path.realpath(executable)
    try:
        st = os.stat(real)
        return (real, st.st_mtime_ns, st.st_size)
    except OSError:
        return (real, None, None)


def fingerprint(env: dict | None = None) -> tuple:
    exe = find_kimi(env)
    return _fingerprint(exe) if exe else ("", None, None)


def _probe_help(executable: str) -> str:
    """``kimi --help`` output (no login, no model call). A nonzero exit
    raises CalledProcessError: a CLI that fails its own help isn't trusted
    to honour the flags it lists."""
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
    """Path of a Kimi CLI that supports every isolation flag, or raise
    KimiRuntimeError saying what to do. A passing result is cached until
    the executable changes."""
    exe = find_kimi(env)
    if not exe:
        raise KimiRuntimeError(
            f"Kimi CLI not found on PATH. {INSTALL_HINT}")
    key = _fingerprint(exe)
    with _capable_lock:
        if _capable.get(key):
            return exe
    try:
        help_text = _probe_help(exe)
    except (OSError, subprocess.SubprocessError) as e:
        raise KimiRuntimeError(
            f"Could not run '{exe} --help' to check the Kimi CLI ({type(e).__name__}). "
            "Sellbird sends no prompt until it can confirm the CLI isolates calls."
        ) from e
    missing = missing_flags(help_text)
    if missing:
        raise KimiRuntimeError(
            f"This Kimi CLI ({exe}) lacks {', '.join(missing)}, which Sellbird needs "
            "to keep its brain calls tool-less. Update it ('kimi upgrade') and "
            "Sellbird resumes on its own. No prompt was sent.")
    with _capable_lock:
        _capable[key] = True
    return exe


def clear_cache():
    with _capable_lock:
        _capable.clear()
