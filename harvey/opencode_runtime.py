"""OpenCode adapter — Harvey's opt-in brain on the operator's OpenCode plan.

Every call runs a short-lived, fully isolated ``opencode`` CLI process: a
private temporary config, data, cache and state directory, an empty
workspace, and a session database the CLI itself creates and Harvey seeds
with ONLY the credential(s) the configured model's provider needs. All of it
is thrown away after the call. That keeps the operator's own OpenCode setup
(their config, agents, plugins, MCP servers, sessions, prompt history, logs)
out of Harvey's calls, and keeps Harvey out of theirs: nothing Harvey does is
recorded in the operator's OpenCode data, and the operator's login is never
written by a Harvey call.

Prompts carry untrusted text (prospect replies, scraped pages), so the model
must have nothing to act with, and the login must only ever reach its own
provider. Before any prompt is sent, the runtime has to prove it:

0. The CLI must be a major version whose flags and behaviour Harvey has
   verified (v2), found on PATH, with every flag the runtime depends on
   (``--standalone``, ``--format``, ``--agent``, ``--model``) present in the
   ``run`` command's help.
1. ``debug agents`` — with NO operator data anywhere in the runtime — must
   report the ``harvey`` agent as a primary agent whose last-matching
   permission rule resolves to ``deny`` for every tool Harvey knows OpenCode
   to have (and for the ``*`` wildcard). Rules are evaluated last-match-wins,
   so this reads what will actually apply, not just what was written.
2. ``debug config`` must read Harvey's private config document from inside
   the temporary home (share disabled) and NO configuration document from
   outside it: an operator config layer sneaking in fails the call.
3. The CLI-created session database is seeded with only the active
   credential row whose integration id matches the model's provider ref,
   read once per call from the operator's session database in read-only mode
   (falling back to that provider's entry in ``auth.json``). A provider with
   no stored credential fails before anything is sent.
4. During the turn, any ``tool`` part in the model's event stream means the
   model tried to act — even one that permissions would refuse, which is
   itself a failed isolation. The process group is killed at once and the
   call fails closed.

Errors are classified for the Brain (see harvey/brain.py): setup/auth/
isolation/model problems stop calls until the operator acts, a usage limit
pauses the provider for a while (OpenCode's free-limit replies carry the
reset in ``retryAfterMs``), and transient runtime failures are retried with
backoff. Hooks exclude OpenCode on Windows, where OpenCode runs under WSL:
the Claude provider covers Windows.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess as _sp
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

logger = logging.getLogger("harvey.opencode")

AGENT_ID = "harvey"
RUNTIME_PREFIX = "harvey-opencode-"
SUPPORTED_MAJOR = 2
OPENCODE_INSTALL_HINT = (
    "Install OpenCode (https://opencode.ai/docs/install): 'curl -fsSL "
    "https://opencode.ai/install | bash' (or 'npm install -g opencode-ai', "
    "'brew install sst/tap/opencode')."
)
# A model Harvey has verified as text-capable and reachable on the operator's
# OpenCode subscription. Setup suggests it; any model ref `opencode run`
# accepts works (see 'opencode models'). reasoning_effort is Codex-only; a
# variant is written into the model ref itself (provider/model#variant).
OPENCODE_SUGGESTED_MODEL = "opencode/gpt-5.3-codex"

# Flags the run command depends on. Before anything is sent, the CLI must
# still offer all of them: a changed surface is refused (setup error) rather
# than run with guessed flags.
REQUIRED_RUN_FLAGS = ("--standalone", "--format", "--agent", "--model")

# Stream limits: a single event line and the whole turn's stream are both
# bounded, and a breach means the CLI is not in the documented protocol.
MAX_PROTOCOL_LINE_BYTES = 8 * 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024

KILL_REAP_SECONDS = 10.0  # bounded wait for a killed process group to exit

# Platforms the OpenCode provider supports (same as Codex; Claude covers
# Windows). Module-level so tests can exercise the refusal on any OS.
_PLATFORM = sys.platform


def require_supported_platform() -> None:
    """OpenCode refuses to run on Windows; Claude is the provider there."""
    if _PLATFORM == "darwin" or _PLATFORM.startswith("linux"):
        return
    raise OpenCodeError(
        "Harvey's OpenCode provider supports macOS and Linux only; it isn't "
        "supported on Windows yet. Use the Claude provider there "
        "(brain.provider: claude in harvey.yaml).",
        kind="setup",
    )


# Everything else — notably the operator's OPENCODE_*/OPENAI/ANTHROPIC/GCP
# variables and their OpenCode CLI state — is dropped from the child, so its
# own variables can never redirect, unlock or reconfigure Harvey's runtime.
_ENV_ALLOWLIST = frozenset({
    "PATH", "LANG", "LANGUAGE", "TZ", "TERM", "USER", "LOGNAME",
    "TMPDIR", "TMP", "TEMP",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
})

# Permission actions to attest: every key OpenCode's shipped docs name for its
# tools, plus the ``*`` wildcard. The last matching rule wins, so after
# writing {"*": "deny"} the resolved effect must be deny for each of these.
ATTEST_ACTIONS = (
    "*", "shell", "read", "edit", "write", "glob", "grep", "list", "bash",
    "task", "todowrite", "todoread", "external_directory", "webfetch",
    "websearch", "lsp", "skill", "question", "doom_loop", "browser",
)


class OpenCodeError(Exception):
    """An OpenCode call failed. ``kind`` drives retry and operator help.

    Message texts name the operator action to take but never carry prompts,
    replies, credential values or raw CLI output.
    """

    def __init__(self, message: str, kind: str = "runtime", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def _setup_error(message: str) -> OpenCodeError:
    return OpenCodeError(message, kind="setup")


def _auth_error(message: str) -> OpenCodeError:
    return OpenCodeError(message, kind="auth")


def _isolation_error(message: str) -> OpenCodeError:
    return OpenCodeError(
        f"OpenCode isolation could not be verified: {message}. "
        "Nothing was used from this attempt.",
        kind="isolation",
    )


def _model_error(message: str) -> OpenCodeError:
    return OpenCodeError(message, kind="model")


def _timeout_error() -> OpenCodeError:
    return OpenCodeError(
        "OpenCode call timed out.", kind="timeout", retryable=True)


@dataclass
class ModelRef:
    """``provider/model`` with an optional ``#variant``."""

    provider_id: str
    model_id: str
    variant: str = ""

    def cli_ref(self) -> str:
        base = f"{self.provider_id}/{self.model_id}"
        return f"{base}#{self.variant}" if self.variant else base


_MODEL_REF_RE = re.compile(
    r"^(?P<provider>[A-Za-z0-9._-]+)/(?P<model>[A-Za-z0-9._:/\[\]-]+?)"
    r"(?:\#(?P<variant>[A-Za-z0-9._-]+))?$"
)


def parse_model_ref(model: str) -> ModelRef:
    """Parse a ``provider/model`` (``#variant`` optional) model ref.

    Raises OpenCodeError(kind="model") on anything that isn't one: the value
    is passed to a CLI, so it must never look like a flag or carry shell
    metacharacters (harvey.config enforces the same guarantee on the config
    side).
    """
    raw = str(model or "").strip()
    match = _MODEL_REF_RE.match(raw)
    if not match:
        raise _model_error(
            f"'{raw}' is not an OpenCode model ref. Use provider/model "
            "(optionally provider/model#variant), e.g. "
            f"'{OPENCODE_SUGGESTED_MODEL}'. See 'opencode models'.")
    ref = ModelRef(match.group("provider"), match.group("model"),
                   match.group("variant") or "")
    if "/" in ref.model_id and ref.provider_id != "openrouter":
        raise _model_error(
            f"'{raw}' has a slash inside the model id. Harvey supports that "
            "shape for openrouter only (e.g. openrouter/minimax/minimax-m3).")
    return ref


def data_home() -> Path:
    """The operator's OpenCode data directory (honors XDG_DATA_HOME)."""
    override = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(override).expanduser() if override else Path.home() / ".local" / "share"
    return base / "opencode"


def find_opencode() -> str | None:
    return shutil.which("opencode")


def _guard_source_db(path: Path, purpose: str) -> None:
    """The operator's own OpenCode data must be safe to borrow from.

    Ownership matters most (a different account's database must never be
    copied into a runtime); group/world readability is only logged, since the
    file is the operator's own session store and Harvey copies one credential
    row into a throwaway runtime — it does not move the file.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        raise _auth_error(
            f"No OpenCode session database at {path}. Open OpenCode once on "
            "this machine (or run 'opencode auth login' and sign in with the "
            f"provider {purpose} needs), then use Harvey again.")
    except OSError as e:
        raise _setup_error(f"Cannot access {path}: {e.strerror or e}")
    if not stat.S_ISREG(st.st_mode):
        raise _setup_error(f"{path} is not a regular file.")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise _setup_error(
            f"{path} is not owned by the user running Harvey; refusing to "
            "borrow its credentials.")
    if st.st_mode & 0o077:
        logger.warning(
            "%s is readable by other users (mode %o). Harvey copies one "
            "credential row into its isolated runtime; fixing the file "
            "permissions is still worth doing.", path, st.st_mode & 0o777)


def check_native_login(model_ref: ModelRef, source: Path | None = None) -> None:
    """Prove the operator has a usable login for the model's provider.

    Read-only: the operator's session database (and, when needed, auth.json)
    is opened but never written. Raises OpenCodeError(kind="auth"|"setup")
    with an actionable message. Used by setup; every call re-checks.
    """
    require_supported_platform()
    home = source or data_home()
    _guard_source_db(home / "opencode.db", model_ref.provider_id)
    _load_credentials(model_ref.provider_id)


def _source_rows(provider_id: str, home: Path | None = None) -> list[dict]:
    """All columns of the operator's active credential rows for one
    integration, read-only, as dicts keyed by column name (WITHOUT values in
    logs). Empty when the database has no such rows (auth.json is tried next
    by `_load_credentials`)."""
    home = home or data_home()
    db_path = home / "opencode.db"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except (OSError, sqlite3.Error) as e:
        raise _setup_error(
            f"Could not read the OpenCode session database at {db_path}: "
            f"{type(e).__name__}")
    try:
        cur = con.execute(
            "SELECT * FROM credential "
            "WHERE integration_id = ? AND active = 1 ORDER BY time_created",
            (provider_id,),
        )
        cols = [c[0] for c in cur.description or []]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _load_credentials(provider_id: str, home: Path | None = None) -> list[dict]:
    """Read the active credential values for one integration, read-only.

    Returns [{"id","value"}, ...]. Never logs values. Sources, in order: the
    OpenCode session database's credential table (what the app's own
    Integrations page writes — a subscription login for the `opencode`
    provider lives ONLY here), then auth.json for API-key entries it doesn't
    carry. Raises an auth error with a path forward when there is neither.
    """
    home = home or data_home()
    rows = _source_rows(provider_id, home)
    creds = [{"id": str(r["id"]), "value": str(r["value"])}
             for r in rows if r.get("id") and r.get("value")]
    if creds:
        return creds
    # auth.json fallback: provider API keys live here
    # ({"openrouter": {"type": "api", "key": ...}}).
    auth_file = home / "auth.json"
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    entry = data.get(provider_id) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        value = entry.get("key") or entry.get("token") or entry.get("access")
        if isinstance(value, str) and value:
            return [{"id": f"auth:{provider_id}", "value": value}]
    raise _auth_error(
        f"OpenCode has no active login for provider '{provider_id}', which "
        "your configured model needs. Run 'opencode auth login', sign in "
        f"with {provider_id}, then use Harvey again.")


def subprocess_run_sync(cmd: list[str], timeout: float = 60.0,
                        cwd: str | os.PathLike | None = None,
                        env: dict | None = None,
                        stdin_bytes: bytes = b"") -> _sp.CompletedProcess:
    """Small sync helper for the one-shot surface probes (setup path).

    Deliberately sync: these run before any runtime exists and probe only
    `--version`/`--help` output — no operator data is involved.
    """
    return _sp.run(cmd, timeout=timeout, cwd=cwd, env=env, stdout=_sp.PIPE,
                   stderr=_sp.PIPE, input=stdin_bytes)


def check_version(exe: str = "") -> str:
    """The installed CLI's version string; refuses anything but v2.x.

    Raises OpenCodeError(kind="setup") when the CLI is missing, unusable, or
    its major version isn't one Harvey's surface checks cover.
    """
    exe = exe or find_opencode()
    if not exe:
        raise _setup_error(
            f"OpenCode CLI not found. {OPENCODE_INSTALL_HINT} Then run "
            "'opencode auth login' with the provider your model needs, and "
            "restart Harvey.")
    try:
        proc = subprocess_run_sync([exe, "--version"])
    except OSError as e:
        raise _setup_error(f"OpenCode CLI at {exe} can't run: {e.strerror or e}")
    text = proc.stdout.decode(errors="replace")
    if proc.returncode != 0:
        raise _setup_error(
            f"'opencode --version' failed (exit {proc.returncode}); the CLI "
            f"install may be damaged. {OPENCODE_INSTALL_HINT}")
    match = re.search(r"\bv?(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise _setup_error(
            f"'opencode --version' reported {text.strip()[:80]!r}, which "
            "isn't a version Harvey can verify.")
    major = int(match.group(1))
    if major != SUPPORTED_MAJOR:
        raise _setup_error(
            f"OpenCode CLI version {major}.x isn't supported by this Harvey "
            f"release (verified surface: v{SUPPORTED_MAJOR}.x). Install "
            f"v{SUPPORTED_MAJOR}.x or update Harvey, then retry.")
    return f"{major}.{match.group(2)}.{match.group(3)}"


def _probe_run_help(exe: str) -> None:
    """`run --help` must still offer every flag the runtime relies on."""
    proc = subprocess_run_sync([exe, "run", "--help"])
    text = (proc.stdout + proc.stderr).decode(errors="replace")
    missing = [flag for flag in REQUIRED_RUN_FLAGS if flag not in text]
    if missing:
        raise _setup_error(
            "The OpenCode CLI no longer exposes the flags Harvey's isolation "
            f"relies on ({', '.join(missing)}). Nothing was sent. Update "
            "OpenCode and Harvey so they match, then try again.")


def check_cli(exe: str = "") -> str:
    """Setup entry point: the CLI is a supported version AND still offers the
    flags the runtime depends on. Returns the version string."""
    exe = exe or find_opencode() or ""
    version = check_version(exe)
    _probe_run_help(exe)
    return version


@dataclass
class OpenCodeResult:
    text: str
    model: str = ""
    session_id: str = ""
    duration_ms: int = 0
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0


def child_env(home: Path) -> dict:
    """A fresh, allowlisted environment for one isolated CLI process.

    OpenCode reads config out of ``$HOME/.config/opencode`` and per-session
    data out of ``$HOME/.local/share/opencode``, so HOME moves too: any
    ``~/.opencode``-style install overlay the operator has and every
    OPENCODE_* environment variables stay out of the runtime. The four XDG
    variables pin the same layout explicitly.
    """
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if os.environ.get(k)}
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "NO_COLOR": "1",
        # Keys the OpenCode CLI checks to detect it's answering an agent, on
        # purpose: keep TTY-tuned behaviour out of Harvey's non-interactive
        # single turn.
        "AI_AGENT": "harvey",
        "OPENCODE": "1",
    })
    return env


CONFIG = {
    # Written into every runtime. Unknown keys are ignored upstream; when all
    # three statements below ever disagree, `debug agents` still attests the
    # resolved answer before any prompt is sent (fail-closed).
    "$schema": "https://opencode.ai/config.json",
    # Never offer the session for sharing: a brain turn must not become a
    # web view.
    "share": "disabled",
    # The runtime only ever runs Harvey's agent; no user plugins load.
    "plugin": [],
    # Permission rules match tool names as wildcard patterns and are
    # last-match-wins. One deny rule for every tool, every agent; the agent
    # entry states it again, and `tools` (the legacy per-agent switch) a
    # third time, so disabling doesn't hinge on any single spelling.
    "permission": {"*": "deny"},
    "agent": {
        AGENT_ID: {
            "description": (
                "Harvey's text-only brain: it reads a prompt and answers in "
                "plain text. It has no tools, no acting, no external calls."),
            "mode": "primary",
            "permission": {"*": "deny"},
            "tools": {"*": False},
        },
    },
}


def _last_effect(rules: list, action: str, resource: str = "*") -> str:
    """The effect the ruleset actually applies to one (tool, resource) pair.

    Rules read like {"action","resource","effect"} and the LAST matching rule
    wins, so a default allow-all plus a trailing deny resolves to deny.
    Returns the winning effect or "" when no rule matches.
    """
    effect = ""
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        r_action = str(rule.get("action") or "")
        r_resource = str(rule.get("resource") or "")
        if (r_action == "*" or fnmatchcase(action, r_action)) and \
                (r_resource == "*" or fnmatchcase(resource, r_resource)):
            effect = str(rule.get("effect") or "")
    return effect


def attest_agent(agent) -> None:
    """The harvey agent must be primary, with deny resolving for every tool."""
    if not isinstance(agent, dict):
        raise _isolation_error("the agent listing had no usable harvey entry")
    if agent.get("mode") != "primary":
        raise _isolation_error("the harvey agent isn't a primary agent")
    rules = agent.get("permissions")
    if not isinstance(rules, list) or not rules:
        raise _isolation_error("the harvey agent reported no permission rules")
    for action in ATTEST_ACTIONS:
        effect = _last_effect(rules, action)
        if effect != "deny":
            raise _isolation_error(
                f"tool permission '{action}' resolves to "
                f"{'nothing' if not effect else effect!r}, not deny")


async def _cli_probe_json(exe, args, runtime, deadline) -> list | dict:
    """Run one offline CLI probe inside the runtime; parse its JSON stdout."""
    remaining = deadline - time.monotonic()
    proc = await _spawn(exe, args, runtime)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=remaining)
    except TimeoutError:
        await _stop(proc)
        raise _isolation_error(
            f"'{' '.join(args)}' didn't finish inside the runtime's deadline")
    if proc.returncode != 0:
        raise _isolation_error(
            f"'{' '.join(args)}' failed inside the runtime (exit "
            f"{proc.returncode})")
    try:
        return json.loads(out.decode("utf-8"))
    except ValueError as e:
        raise _isolation_error(
            f"'{' '.join(args)}' didn't report JSON (a changed CLI surface?)"
        ) from e


def _attest_agent_listing(payload) -> None:
    """`debug agents` in the credential-less runtime must attest ``harvey``."""
    entry = next(
        (a for a in payload if isinstance(a, dict) and a.get("id") == AGENT_ID),
        None,
    ) if isinstance(payload, list) else None
    if entry is None:
        raise _isolation_error(
            "the harvey agent wasn't in the runtime's agent listing")
    attest_agent(entry)


def _attest_config_documents(payload, runtime) -> None:
    """The runtime must resolve ONLY config documents inside its temp home.

    Every document or directory the CLI would rule from must live inside the
    throwaway runtime root, and Harvey's own private config must be among
    them with sharing off. Anything else (an operator layer, a system layer,
    another user's file) fails closed before a prompt exists.
    """
    if not isinstance(payload, list) or not payload:
        raise _isolation_error("'debug config' reported no documents")
    prefix = str(runtime.root) + os.sep
    saw_own_config = False
    for node in payload:
        if not isinstance(node, dict):
            raise _isolation_error("'debug config' reported an unusable entry")
        node_path = str(node.get("path") or "")
        if not node_path.startswith(prefix):
            raise _isolation_error(
                "a configuration layer lives outside Harvey's isolated "
                "runtime and was refused")
        if node.get("type") == "document" and \
                Path(node_path) == runtime.config_path:
            info = node.get("info")
            if isinstance(info, dict) and info.get("share") == "disabled":
                saw_own_config = True
    if not saw_own_config:
        raise _isolation_error(
            "Harvey's private config wasn't among the documents the runtime "
            "would read")


def _seed_runtime_db(runtime, ref: ModelRef, native_home: Path | None) -> int:
    """Copy ONLY the model provider's credential into the runtime's database.

    The runtime database was created by the CLI itself moments earlier; the
    operator's is opened read-only. Returns the number of rows seeded. Any
    surprise (missing skeleton, missing table, unexpected existing rows)
    fails closed.
    """
    native = native_home or data_home()
    if not runtime.db_path.is_file():
        raise _isolation_error(
            "the isolated CLI didn't create its own session database, so "
            "Harvey can't seed just one provider's login")
    rows = _source_rows(ref.provider_id, native)
    synthetic = not rows
    if synthetic:  # auth.json entry (API-key providers): build the row Harvey inserts
        creds = _load_credentials(ref.provider_id, native)
        rows = [{
            "id": f"cred-harvey-{c['id']}"[:120],
            "integration_id": ref.provider_id,
            "label": "Harvey isolated runtime (borrowed from auth.json)",
            "value": c["value"],
            "active": 1,
        } for c in creds]
    con = sqlite3.connect(runtime.db_path)
    seeded = 0
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(credential)")]
        for required in ("id", "integration_id", "value"):
            if required not in cols:
                raise _isolation_error(
                    "the runtime session database has no usable credential "
                    "table")
        now = int(time.time() * 1000)
        for row in rows:
            values = {col: row[col] for col in row if col in cols}
            values.setdefault("id", f"cred-harvey-{uuid.uuid4().hex[:16]}")
            values.setdefault("integration_id", ref.provider_id)
            values.setdefault("label", "Harvey isolated runtime")
            values["value"] = row["value"]
            values.setdefault("active", 1)
            values.setdefault("time_created", now)
            values.setdefault("time_updated", now)
            names = ", ".join(values)
            marks = ", ".join("?" * len(values))
            con.execute(
                f"INSERT INTO credential ({names}) VALUES ({marks})",
                list(values.values()))
            seeded += 1
        con.commit()
        # Attest what actually landed: only this integration's rows, nothing
        # else came along for the ride.
        (seed_total,) = con.execute("SELECT COUNT(*) FROM credential").fetchone()
        (other,) = con.execute(
            "SELECT COUNT(*) FROM credential WHERE integration_id != ? "
            "OR integration_id IS NULL",
            (ref.provider_id,),
        ).fetchone()
        if seeded != len(rows) or seed_total != seeded or other:
            raise _isolation_error(
                "the runtime session database isn't holding exactly "
                f"{ref.provider_id}'s credentials and nothing else")
    except OpenCodeError:
        raise
    except sqlite3.Error as e:
        raise _isolation_error(
            f"seeding the runtime session database failed ({type(e).__name__})"
        ) from e
    finally:
        con.close()
    if synthetic:
        logger.info(
            "Seeded the runtime with the %s login from auth.json "
            "(no %s rows in the OpenCode database).", ref.provider_id,
            ref.provider_id)
    return seeded


class _RuntimeHome:
    """One throwaway runtime home; ``close()`` removes the whole tree.

    Layout (XDG-style, all under one temp root):

        <root>/home/config/opencode/opencode.json   Harvey's private config
        <root>/home/data/opencode/opencode.db       CLI-created, Harvey-seeded
        <root>/home/{cache,state}                   fresh per call
        <root>/workspace                            the child's cwd: empty
    """

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX))
        self.home = self.root / "home"
        self.config_dir = self.home / "config" / "opencode"
        self.data_dir = self.home / "data" / "opencode"
        self.cache_dir = self.home / "cache"
        self.state_dir = self.home / "state"
        self.workspace = self.root / "workspace"
        for directory in (self.config_dir, self.data_dir, self.cache_dir,
                          self.state_dir, self.workspace):
            directory.mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "opencode.json"
        self.db_path = self.data_dir / "opencode.db"
        self.config_path.write_text(
            json.dumps(CONFIG, indent=2), encoding="utf-8")
        os.chmod(self.config_path, 0o600)

    @property
    def env(self) -> dict:
        return child_env(self.home)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


async def _spawn(exe: str, args: list[str], runtime: _RuntimeHome):
    """Start one CLI process in the runtime: empty workspace, allowlisted
    environment, its own process group (so teardown can kill descendants)."""
    return await asyncio.create_subprocess_exec(
        exe, "--standalone", *args,
        cwd=str(runtime.workspace),
        env=runtime.env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=MAX_PROTOCOL_LINE_BYTES,
    )


def _kill_group(proc) -> None:
    """Kill the child and everything under it (best effort)."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


async def _stop(proc) -> None:
    """Killed process reaping, bounded so shutdown can't hang on a child."""
    _kill_group(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=KILL_REAP_SECONDS)
    except Exception:
        pass


async def _feed_stdin(proc, prompt: str) -> None:
    """Hand the prompt over stdin (never argv, which is visible to other
    local users) and close the pipe. A broken pipe is fine: the CLI may exit
    early; its stdout then carries a usable error event."""
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _num(value) -> int:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def _classify_stream_error(error) -> OpenCodeError:
    """One stream ``error`` event -> the failure the Brain can act on.

    Verified shapes (OpenCode v2.x): {"type": "provider.free-limit",
    "retryAfterMs": ...} when the plan's free usage is spent; "provider.auth"
    with a country message for region-blocked models; "provider.no-route"
    when no credential reaches the model. Everything else is treated as a
    transient runtime failure.
    """
    if not isinstance(error, dict):
        return OpenCodeError(
            "OpenCode reported an unusable error event.",
            kind="runtime", retryable=True)
    etype = str(error.get("type") or "")
    message = str(error.get("message") or "")
    if "free-limit" in etype or "free limit" in message.lower():
        e = OpenCodeError(
            "OpenCode usage limit reached for this plan; Harvey will resume "
            "after the reset OpenCode announced.",
            kind="usage_limit")
        e.retry_after_ms = _num(error.get("retryAfterMs") or
                                error.get("retry_after_ms"))
        return e
    if "no-route" in etype:
        return _model_error(
            "OpenCode reported the configured model as unavailable with the "
            "login Harvey found (provider.no-route). Check the model ref in "
            "harvey.yaml ('opencode models' lists what's available) and that "
            "the plan is signed in ('opencode auth login'), then use Harvey "
            "again.")
    if "auth" in etype or "auth" in message.lower():
        if "country" in message.lower() or "region" in message.lower():
            return _model_error(
                "OpenCode reports the configured model isn't available in "
                "your country/region. Choose another model in harvey.yaml.")
        return _auth_error(
            "OpenCode rejected the login. Run 'opencode auth login' with "
            "your provider, then use Harvey again.")
    if "rate" in etype.lower() or "429" in message[:8]:
        e = OpenCodeError(
            "OpenCode rate-limited the plan briefly.",
            kind="usage_limit", retryable=True)
        e.retry_after_ms = _num(error.get("retryAfterMs"))
        return e
    return OpenCodeError(
        "OpenCode call failed with a provider error.",
        kind="runtime", retryable=True)


def _attest_turn_usage(part) -> dict:
    """One step_finish part -> token/cost counts for Harvey's ledger."""
    usage = {}
    if isinstance(part, dict):
        if isinstance(part.get("tokens"), dict):
            usage["input_tokens"] = _num(part["tokens"].get("input"))
            usage["output_tokens"] = _num(part["tokens"].get("output"))
            cache = part["tokens"].get("cache")
            if isinstance(cache, dict):
                usage["cache_read_tokens"] = _num(cache.get("read"))
                usage["cache_creation_tokens"] = _num(cache.get("write"))
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and cost >= 0:
            usage["cost_usd"] = float(cost)
    return usage


class OpenCodeRuntime:
    """Runs isolated, tool-less OpenCode turns on the operator's plan.

    One throwaway runtime per turn: temporary config/data/cache/state, empty
    workspace, allowlisted environment. Attested before any prompt is sent,
    then seeded with only the one provider's credential the model needs.
    """

    def __init__(self, model: str = "", opencode_path: str | None = None,
                 native_home: Path | None = None):
        # An empty model stays deterministic: OpenCode's own "use my recent
        # pick" default would read operator state Harvey deliberately
        # excludes, so an unconfigured Harvey thinks with the verified
        # subscription-backed model. Setup suggests it; operators override.
        self.model = model.strip() or OPENCODE_SUGGESTED_MODEL
        self._opencode_path = opencode_path
        self.native_home = native_home
        self._lock: asyncio.Lock | None = None

    def _resolve_binary(self) -> str:
        path = self._opencode_path or find_opencode()
        if not path:
            raise _setup_error(
                f"OpenCode CLI not found. {OPENCODE_INSTALL_HINT} Then run "
                "'opencode auth login' with your provider, and restart "
                "Harvey.")
        return path

    def _serialize(self) -> asyncio.Lock:
        # One OpenCode runtime at a time per Brain.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def run(self, prompt: str, timeout: float, skip_if=None
                  ) -> OpenCodeResult:
        """One isolated turn. ``skip_if`` (returns a reason or "") is checked
        once this call holds the runtime lock, so calls queued behind one
        that discovered a persistent problem don't start runtimes too."""
        return await self._bounded(
            timeout, lambda deadline: self._run(prompt, deadline), skip_if)

    async def preflight(self, timeout: float = 120.0) -> None:
        """Everything a real call checks, short of sending a prompt.
        Raises OpenCodeError."""
        return await self._bounded(timeout, self._preflight)

    async def _preflight(self, deadline: float) -> None:
        exe = self._resolve_binary()
        ref = parse_model_ref(self.model)
        runtime = _RuntimeHome()
        try:
            await self._attested_setup(exe, runtime, ref, deadline)
            # Same seeding a real call does (no turn, so no inference): the
            # operator's benefit when this fails is setup, not a runtime.
            _seed_runtime_db(runtime, ref, self.native_home)
        finally:
            runtime.close()

    async def _bounded(self, timeout: float, work, skip_if=None):
        """One deadline covers waiting for the lock, start-up, attestation,
        seeding, sending, the turn and teardown (teardown adds a bounded
        process-exit wait). Expected failures come out as OpenCodeError;
        cancellation always propagates."""
        require_supported_platform()
        deadline = time.monotonic() + timeout
        try:
            async with asyncio.timeout(timeout):
                async with self._serialize():
                    reason = skip_if() if skip_if else ""
                    if reason:
                        raise OpenCodeError(reason, kind="paused")
                    return await work(deadline)
        except OpenCodeError:
            raise
        except TimeoutError:
            raise _timeout_error() from None
        except (OSError, ValueError, RuntimeError, EOFError) as e:
            raise OpenCodeError(
                f"OpenCode runtime failed ({type(e).__name__}).",
                kind="runtime", retryable=True) from None

    async def _attested_setup(self, exe: str, runtime: _RuntimeHome,
                              ref: ModelRef, deadline: float) -> None:
        """Prove the runtime is locked down before it holds any login."""
        # Surface checks happen off-loop, before credentials exist anywhere.
        await asyncio.to_thread(check_version, exe)
        await asyncio.to_thread(_probe_run_help, exe)

        # Phase 1 — no credentials anywhere in the runtime.
        agents = await _cli_probe_json(exe, ["debug", "agents"], runtime,
                                       deadline)
        _attest_agent_listing(agents)
        config_docs = await _cli_probe_json(exe, ["debug", "config"], runtime,
                                            deadline)
        _attest_config_documents(config_docs, runtime)
        if not runtime.db_path.is_file():
            raise _isolation_error(
                "the isolated CLI didn't create its own session database, so "
                "Harvey can't seed just one provider's login")

    async def _run(self, prompt: str, deadline: float) -> OpenCodeResult:
        exe = self._resolve_binary()
        ref = parse_model_ref(self.model)
        runtime = _RuntimeHome()
        started = time.monotonic()
        try:
            await self._attested_setup(exe, runtime, ref, deadline)
            # Only after isolation is proven does the login leave the
            # operator's files — into the throwaway runtime, one provider.
            _seed_runtime_db(runtime, ref, self.native_home)
            # Isolation verified — only now does untrusted text leave Harvey.
            return await self._turn(exe, runtime, ref, prompt, deadline,
                                    started)
        finally:
            runtime.close()

    async def _turn(self, exe, runtime, ref, prompt, deadline, started
                    ) -> OpenCodeResult:
        proc = await _spawn(exe, ["run", "--format", "json",
                                  "--agent", AGENT_ID,
                                  "--model", ref.cli_ref()], runtime)
        text_parts: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_creation_tokens": 0,
                 "cost_usd": 0.0}
        num_turns = 0
        session_id = ""
        stream_bytes = 0
        ended: OpenCodeError | None = None  # the failure that ends the call
        feeder = asyncio.create_task(_feed_stdin(proc, prompt))

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    ended = _timeout_error()
                    break
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining)
                except TimeoutError:
                    ended = _timeout_error()
                    break
                except ValueError:
                    # The reader's hard ceiling is MAX_PROTOCOL_LINE_BYTES:
                    # one event line crossing it is itself a protocol breach.
                    ended = _isolation_error(
                        "the isolated OpenCode stream broke Harvey's "
                        "protocol-size limits")
                    break
                if not line:
                    break  # EOF: the process is finishing
                stream_bytes += len(line)
                if len(line) > MAX_PROTOCOL_LINE_BYTES or \
                        stream_bytes > MAX_STREAM_BYTES:
                    ended = _isolation_error(
                        "the isolated OpenCode stream broke Harvey's "
                        "protocol-size limits")
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    ended = _isolation_error(
                        "the isolated OpenCode stream wasn't the documented "
                        "JSON event protocol")
                    break
                if not isinstance(event, dict):
                    ended = _isolation_error(
                        "the isolated OpenCode stream wasn't the documented "
                        "JSON event protocol")
                    break
                sid = event.get("sessionID")
                if isinstance(sid, str) and sid:
                    session_id = sid
                etype = str(event.get("type") or "")
                part = event.get("part") if isinstance(
                    event.get("part"), dict) else {}
                if etype == "text":
                    chunk = part.get("text")
                    if isinstance(chunk, str) and chunk:
                        text_parts.append(chunk)
                elif etype == "tool":
                    ended = _isolation_error(
                        f"OpenCode attempted a {part.get('tool') or 'tool'} "
                        "call; the call was aborted and nothing from this "
                        "attempt was used")
                    break
                elif etype == "error":
                    ended = _classify_stream_error(event.get("error"))
                    break
                elif etype == "step_finish":
                    num_turns += 1
                    # Each response step reports its own tokens/cost; a turn
                    # can span several, so the ledger sums them.
                    for key, value in _attest_turn_usage(part).items():
                        usage[key] = usage.get(key, 0) + value
        except BaseException:
            # Shutdown or cancellation: kill the process group before the
            # runtime directory disappears, and reap with a bound.
            feeder.cancel()
            await _stop(proc)
            raise
        finally:
            if not feeder.done():
                feeder.cancel()

        if ended is not None:
            await _stop(proc)
            raise ended
        await _stop(proc)

        if proc.returncode not in (0, None):
            raise OpenCodeError(
                "OpenCode call ended with an error (no usable events).",
                kind="runtime", retryable=True)
        if not text_parts:
            raise OpenCodeError(
                "OpenCode finished without an answer.", kind="runtime",
                retryable=True)
        return OpenCodeResult(
            text="".join(text_parts),
            model=ref.cli_ref(),
            session_id=session_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            num_turns=num_turns,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_tokens"],
            cache_creation_tokens=usage["cache_creation_tokens"],
            cost_usd=round(usage["cost_usd"], 6),
        )

    def fingerprint(self) -> tuple:
        """File metadata (never contents) of the operator's session database,
        auth.json and the OpenCode executable. It changes when the operator
        renews a login or reinstalls OpenCode, which is when a paused
        provider is worth re-checking."""
        def meta(path):
            try:
                st = os.stat(path)
            except (OSError, TypeError, ValueError):
                return None
            return (st.st_ino, st.st_size, st.st_mtime_ns)

        native = self.native_home or data_home()
        return (meta(native / "opencode.db"),
                meta(native / "auth.json"),
                meta(self._opencode_path or find_opencode()))
