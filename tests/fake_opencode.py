"""Scripted stand-in for the `opencode` CLI, used by tests/test_opencode.py.

Usage: python fake_opencode.py <scenario> <log_path> <opencode args...>

Speaks exactly the protocol Harvey's adapter verified on OpenCode v2.x:
`--version`, `run --help`, `debug agents` (which creates the session-database
skeleton under $XDG_DATA_HOME/opencode), `debug config` (the config documents
the CLI would rule from), and `run --format json --agent <id> --model <ref>`
(streamed JSON events, prompt on stdin). It logs every invocation — args and
what the seeded runtime database holds — to <log_path>, one JSON object per
line, but never the prompt itself, so tests can assert what was — and
wasn't — sent.
"""

import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import time

SCENARIO, LOG = sys.argv[1], sys.argv[2]
ARGS = sys.argv[3:]
# Harvey's runtime invokes the CLI as `opencode --standalone <cmd ...>`.
ARGS = ARGS[1:] if ARGS[:1] == ["--standalone"] else ARGS

VERSION = {"jump_version": "opencode v3.4.5"}.get(SCENARIO, "opencode v2.2.5")

HELP_TEXT = """\
DESCRIPTION
  Run OpenCode with a message

USAGE
  opencode run [flags] [<message...>]

FLAGS
  --model, -m string    Model to use in the format provider/model#variant
  --agent string        Agent to use
  --format choice       Output format (choices: default, json)

GLOBAL FLAGS
  --help, -h            Show help information
  --version, -v         Show version information
  --wizard              Start wizard mode for a command
  --standalone          Run with a private server instead of the background service
"""

# The config allowlist means FAKE_OPENCODE_* env vars never reach the fake;
# argv carries scenario + log, and the operator (fixture) writes the login
# where the runtime's XDG_DATA_HOME points — the way a real install would.

AGENT_OK = {
    "id": "harvey", "name": "harvey", "mode": "primary",
    "permissions": [
        {"action": "*", "resource": "*", "effect": "allow"},  # built-in default
        {"action": "read", "resource": "*.env", "effect": "ask"},
        {"action": "*", "resource": "*", "effect": "deny"},   # Harvey's: last wins
    ],
}

AGENT_ATTEST_FAIL = [
    {"id": "build", "name": "build", "mode": "primary", "permissions": []},
    # Harvey's rule list resolves to allow: attestation must catch it.
    {"id": "harvey", "name": "harvey", "mode": "primary",
     "permissions": [
         {"action": "*", "resource": "*", "effect": "deny"},
         {"action": "*", "resource": "*", "effect": "allow"},
     ]},
]

AGENTS_NO_HARVEY = [
    {"id": "build", "name": "build", "mode": "primary", "permissions": []},
    {"id": "plan", "name": "plan", "mode": "primary", "permissions": []},
]


def log(entry):
    entry["t"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def data_dir():
    override = os.environ.get("XDG_DATA_HOME", "")
    base = pathlib.Path(override) if override else pathlib.Path.home() / ".local/share"
    return base / "opencode"


def config_path():
    override = os.environ.get("XDG_CONFIG_HOME", "")
    base = pathlib.Path(override) if override else pathlib.Path.home() / ".config"
    return base / "opencode" / "opencode.json"


def read_prompt_and_close_stdin():
    try:
        return sys.stdin.read()
    finally:
        try:
            sys.stdin.close()
        except Exception:
            pass


if ARGS == ["--version"]:
    log({"cmd": "--version"})
    print(VERSION)
    sys.exit(0)

if ARGS == ["run", "--help"]:
    text = HELP_TEXT
    if SCENARIO == "missing_flag":
        text = text.replace("--agent string        Agent to use\n", "")
    log({"cmd": "run --help", "flags_present": {
        flag: flag in text for flag in ("--standalone", "--format", "--agent", "--model")}})
    print(text)
    sys.exit(0)

if ARGS == ["debug", "agents"]:
    # Like the real CLI, creating agents state bootstraps the session
    # database skeleton Harvey then seeds with one provider's credential.
    if SCENARIO not in ("no_db",):
        db_dir, db = data_dir(), data_dir() / "opencode.db"
        db_dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE IF NOT EXISTS credential (
              id text PRIMARY KEY,
              integration_id text,
              label text NOT NULL,
              value text NOT NULL,
              connector_id text,
              method_id text,
              active integer,
              time_created integer,
              time_updated integer
            )""")
        con.commit()
        con.close()
        log({"cmd": "debug agents", "bootstrapped_db": str(db)})
    else:
        log({"cmd": "debug agents", "bootstrapped_db": None})
    if SCENARIO == "agents_command_fails":
        sys.exit(5)
    listing = AGENTS_NO_HARVEY if SCENARIO == "no_agent" else \
        AGENT_ATTEST_FAIL if SCENARIO == "bad_agent" else [AGENT_OK]
    print(json.dumps(listing))
    sys.exit(0)

if ARGS == ["debug", "config"]:
    own = {"type": "document", "path": str(config_path()),
           "info": {"$schema": "https://opencode.ai/config.json",
                    "share": "disabled", "permissions": [
                        {"action": "*", "resource": "*", "effect": "deny"}]}}
    dir_node = {"type": "directory", "path": str(config_path().parent)}
    scene = SCENARIO
    if scene == "config_leak":
        docs = [own, dir_node,
                {"type": "document", "path": "/etc/opencode/opencode.json",
                 "info": {"share": "disabled"}}]
    elif scene == "config_missing_own":
        docs = [{"type": "document", "path": "/etc/opencode/opencode.json",
                 "info": {"share": "disabled"}}]
    elif scene == "config_share_on":
        # A config Harvey didn't write but that resolves with sharing on:
        # attestation must catch it too.
        docs = [{**own, "info": {**own["info"], "share": "public"}}, dir_node]
    else:
        docs = [own, dir_node]
    log({"cmd": "debug config"})
    print(json.dumps(docs))
    sys.exit(0)

assert ARGS[:1] == ["run"], ARGS
stdin_data = read_prompt_and_close_stdin()

# Post-attestation. Report exactly what the runtime session database holds,
# so tests can prove Harvey seeded one provider's credentials and nothing
# else — before any model output is used.
db = data_dir() / "opencode.db"
runtime_rows = []
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    runtime_rows = [
        {"integration_id": r[0], "active": r[1], "label": r[2]}
        for r in con.execute(
            "SELECT integration_id, active, label FROM credential "
            "ORDER BY integration_id")
    ]
    con.close()
except sqlite3.Error as e:
    runtime_rows = [{"error": type(e).__name__}]
log({"cmd": "run", "argv": ARGS, "runtime_rows": runtime_rows, "pid": os.getpid(),
     "stdin_sha256": hashlib.sha256(stdin_data.encode("utf-8")).hexdigest()})

if SCENARIO == "hang":
    time.sleep(120)
    sys.exit(0)

if SCENARIO == "crash":
    sys.stderr.write("boom\n")
    sys.exit(99)

if SCENARIO == "free_limit":
    send({"type": "error", "sessionID": "ses-1",
          "error": {"type": "provider.free-limit", "retryAfterMs": 12345}})
    sys.exit(1)

if SCENARIO == "region":
    send({"type": "error", "sessionID": "ses-1",
          "error": {"type": "provider.auth",
                    "message": "This model is not available in your country"}})
    sys.exit(1)

if SCENARIO == "auth_broke":
    send({"type": "error", "sessionID": "ses-1",
          "error": {"type": "provider.auth", "message": "unauthorized"}})
    sys.exit(1)

if SCENARIO == "no_route":
    send({"type": "error", "sessionID": "ses-1",
          "error": {"type": "provider.no-route",
                    "message": "Model unavailable: opencode/gpt-5.3-codex"}})
    sys.exit(1)

if SCENARIO == "provider_gone":
    send({"type": "error", "sessionID": "ses-1",
          "error": {"type": "provider.rubber-duck", "message": "???"}})
    sys.exit(1)

if SCENARIO == "bad_json":
    sys.stdout.write("warnings: not json at all\n")
    sys.stdout.flush()
    time.sleep(5)   # never finish: the adapter must abort on the malformed line
    sys.exit(0)

SESSION = {"sessionID": "ses-fake-1", "timestamp": 1}
if SCENARIO == "no_answer":
    send({**SESSION, "type": "step_finish",
          "part": {"type": "step-finish", "reason": "stop", "cost": 0.01,
                   "tokens": {"input": 10, "output": 0,
                              "cache": {"read": 0, "write": 0}}}})
    sys.exit(0)

if SCENARIO == "flood":
    # One enormous line: far beyond the protocol line limit. The adapter
    # must abort without reading any of it as an answer.
    sys.stdout.write("{\"type\":\"text\",\"part\":" + "x" * (8 * 1024 * 1024 + 64)
                     + "}\n")
    sys.stdout.flush()
    time.sleep(20)
    sys.exit(0)

# ok / tool / tool_after_text: the scripted happy path, with optional
# misbehaviour injected where the scenario name says so.
if SCENARIO == "tool":
    send({**SESSION, "type": "text",
          "part": {"type": "text", "text": "Let me run that for you."}})
    send({**SESSION, "type": "tool",
          "part": {"type": "tool", "tool": "shell",
                   "state": {"status": "running"}}})
    # The adapter must kill us before this finishes: nothing further is sent.
    time.sleep(120)
    sys.exit(0)

if SCENARIO == "tool_after_text":
    send({**SESSION, "type": "text",
          "part": {"type": "text", "text": "HARVEY_OPENCODE_OK"}})
    send({**SESSION, "type": "step_finish",
          "part": {"type": "step-finish", "reason": "stop", "cost": 0.01,
                   "tokens": {"input": 10, "output": 21,
                              "cache": {"read": 1, "write": 2}}}})
    send({**SESSION, "type": "tool",
          "part": {"type": "tool", "tool": "bash",
                   "state": {"status": "running"}}})
    time.sleep(120)
    sys.exit(0)

# Happy path: text, then two measured response steps (a turn can span
# several); the ledger must sum them.
send({**SESSION, "type": "text",
      "part": {"type": "text", "text": "HARVEY_OPENCODE_"}})
send({**SESSION, "type": "step_finish",
      "part": {"type": "step-finish", "reason": "stop", "cost": 0.01,
               "tokens": {"input": 10, "output": 21,
                          "cache": {"read": 1, "write": 2}}}})
sys.stdout.flush()
send({**SESSION, "type": "text",
      "part": {"type": "text", "text": "OK"}})
send({**SESSION, "type": "step_finish",
      "part": {"type": "step-finish", "reason": "stop", "cost": 0.02,
               "tokens": {"input": 5, "output": 1,
                          "cache": {"read": 3, "write": 4}}}})
sys.exit(0)
