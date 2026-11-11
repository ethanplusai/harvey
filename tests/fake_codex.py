"""Scripted stand-in for the `codex` CLI, used by tests/test_codex.py.

Usage: python fake_codex.py <scenario> <log_path> <codex args...>

Speaks just enough of the app-server stdio protocol for Sellbird's adapter and
logs every request it receives (method + params) to <log_path>, one JSON
object per line, so tests can assert exactly what was — and wasn't — sent.
"""

import json
import os
import sys
import time
import tomllib

SCENARIO, LOG = sys.argv[1], sys.argv[2]
ARGS = sys.argv[3:]

CATALOG = {"models": [
    {"slug": "gpt-6.1-sol", "visibility": "list", "priority": 1,
     "tool_mode": "code_mode_only", "experimental_supported_tools": ["clock"]},
    {"slug": "gpt-5.5", "visibility": "list", "priority": 13,
     "tool_mode": None, "experimental_supported_tools": []},
]}


def log(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


VERSION = {"old_version": "0.158.0"}.get(SCENARIO, "0.159.3")
AUTH = os.path.join(os.environ["CODEX_HOME"], "auth.json")
# Like real Codex, "smart" gives every call its own thread.
THREAD_ID = f"thr-{os.getpid()}" if SCENARIO == "smart" else "thr-1"


def read_auth():
    try:
        with open(AUTH) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


if ARGS == ["--version"]:
    log({"cmd": "--version", "has_auth": read_auth() is not None})
    print(f"codex-cli {VERSION}")
    sys.exit(0)

if ARGS[:2] == ["debug", "models"]:
    log({"cmd": "debug models", "env_keys": sorted(os.environ), "has_auth": read_auth() is not None,
         "home": os.environ.get("HOME"), "codex_home": os.environ.get("CODEX_HOME")})
    catalog = dict(CATALOG)
    if SCENARIO == "no_toolless_model":
        catalog = {"models": [{**CATALOG["models"][0], "tool_mode": "future_mode"}]}
    if SCENARIO == "unlisted_toolless":
        # The tool-less model is in the catalog but not listed in the picker.
        catalog = {"models": [{**m, "visibility": "hide"} for m in CATALOG["models"]]}
    sys.stdout.write(json.dumps(catalog))
    sys.exit(0)

assert ARGS[:1] == ["app-server"], ARGS
SNAPSHOT = read_auth()
with open(os.path.join(os.environ["CODEX_HOME"], "config.toml"), "rb") as f:
    catalog_path = tomllib.load(f).get("model_catalog_json")
if catalog_path:
    with open(catalog_path) as f:
        log({"private_catalog": json.load(f)})
log({"cmd": "app-server", "pid": os.getpid(), "cwd": os.getcwd(), "argv": ARGS,
     "env_keys": sorted(os.environ), "home": os.environ.get("HOME"),
     "codex_home": os.environ.get("CODEX_HOME"),
     "config_toml": open(os.path.join(os.environ["CODEX_HOME"], "config.toml")).read(),
     "auth": SNAPSHOT, "auth_is_link": os.path.islink(AUTH),
     "home_mode": os.stat(os.environ["HOME"]).st_mode & 0o777,
     "codex_home_mode": os.stat(os.environ["CODEX_HOME"]).st_mode & 0o777,
     "workspace_mode": os.stat(os.getcwd()).st_mode & 0o777,
     "auth_mode_bits": (os.stat(AUTH).st_mode & 0o777) if SNAPSHOT is not None else None})
if SNAPSHOT is not None and SCENARIO == "refresh_in_place":
    # A token refresh written straight into the login file Codex was given.
    with open(AUTH, "r+") as f:
        f.seek(0)
        f.write(json.dumps({"tokens": {"access_token": "new", "refresh_token": "rotated"}}))
        f.truncate()
if SNAPSHOT is not None and SCENARIO == "refresh_atomic":
    # The same refresh written by atomic rename.
    with open(AUTH + ".tmp", "w") as f:
        f.write(json.dumps({"tokens": {"access_token": "new", "refresh_token": "rotated"}}))
    os.replace(AUTH + ".tmp", AUTH)


def effective_config():
    features = {line.split(" = ")[0]: False
                for line in open(os.path.join(os.environ["CODEX_HOME"], "config.toml"))
                .read().split("[features]\n", 1)[1].splitlines() if " = " in line}
    config = {
        "project_doc_max_bytes": 0, "include_environment_context": False,
        "include_permissions_instructions": False, "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False, "web_search": "disabled",
        "forced_login_method": "chatgpt", "cli_auth_credentials_store": "file",
        "approval_policy": "never", "sandbox_mode": "read-only",
        "model_provider": None, "chatgpt_base_url": "https://chatgpt.com/backend-api/",
        "instructions": None, "developer_instructions": None, "openai_base_url": None,
        "notify": [], "hooks": None, "profile": None, "features": features,
        "skills": {"include_instructions": False, "bundled": {"enabled": False}},
        "agents": {"enabled": False}, "mcp_servers": {},
    }
    with open(os.path.join(os.environ["CODEX_HOME"], "config.toml"), "rb") as f:
        config["model_catalog_json"] = tomllib.load(f).get("model_catalog_json")
    if SCENARIO == "catalog_override" and SNAPSHOT is not None:
        config["model_catalog_json"] = "/tmp/foreign-models.json"
    if SCENARIO == "feature_on":
        config["features"]["shell_tool"] = True
    if SCENARIO == "custom_provider":
        config["model_provider"] = "my-proxy"
    if SCENARIO == "notify":
        config["notify"] = ["/bin/sh", "-c", "echo"]
    if SCENARIO == "provider_override":
        config["model_providers"] = {"openai": {"base_url": "https://example.invalid/v1"}}
    if SCENARIO == "chatgpt_endpoint":
        config["chatgpt_base_url"] = "https://example.invalid/backend-api/"
    if SCENARIO == "otel_endpoint":
        config["otel"] = {"exporter": {"otlp-http": {"endpoint": "https://example.invalid"}}}
    if SCENARIO == "unknown_endpoint":
        config["some_future_setting"] = {"url": "wss://example.invalid/socket"}
    if SCENARIO in ("mcp_tools", "mcp_disabled"):
        config["mcp_servers"] = {"inherited": {"command": "x"}}
    return config


def answer_for(prompt):
    """Scenario "smart": a plausible answer per Sellbird prompt type, so whole
    agent flows can run through the real adapter."""
    low = prompt.lower()
    if "respond with exactly: sellbird_ready" in low:
        return "SELLBIRD_READY"
    if "classify this email reply" in low:
        if "remove me" in low:
            return "unsubscribe"
        return "interested" if "pricing" in low else "question"
    if '"delay_days"' in prompt:
        return json.dumps([
            {"step": 1, "subject": "front desk", "delay_days": 0,
             "body": "Hi {{first_name}}, how does {{company}} handle this today?"},
            {"step": 2, "subject": "another angle", "delay_days": 3,
             "body": "{{first_name}}, one more thought on this. Worth a look?"},
            {"step": 3, "subject": "closing the loop", "delay_days": 4,
             "body": "Last note from me, {{first_name}}. Should I stop here?"},
        ])
    if '{"subject": "...", "body": "..."}' in prompt:
        return json.dumps({"subject": "front desk",
                           "body": "Hi Jane, how does Acme handle this today?"})
    if "respond only with valid json" in low:
        return json.dumps({"ok": True, "summary": "fixture"})
    return "Happy to help. Does Thursday at 10 work for a quick call?"


def turn_events(thread_id, turn_id, prompt=""):
    ev = []
    if SCENARIO == "toolcall":
        ev.append({"method": "rawResponseItem/completed", "params": {
            "threadId": thread_id, "turnId": turn_id,
            "item": {"type": "function_call", "name": "exec_command", "arguments": "{}"}}})
        return ev
    if SCENARIO == "command_item":
        ev.append({"method": "item/started", "params": {
            "threadId": thread_id, "turnId": turn_id,
            "item": {"type": "commandExecution", "id": "c1", "command": "rm -rf /"}}})
        return ev
    if SCENARIO == "approval":
        ev.append({"id": "srv-1", "method": "item/commandExecution/requestApproval",
                   "params": {"threadId": thread_id, "turnId": turn_id}})
        return ev
    if SCENARIO == "usage_limit":
        ev.append({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
            "id": turn_id, "items": [], "status": "failed",
            "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"}}}})
        return ev
    if SCENARIO == "hang":
        return ev
    if SCENARIO == "commentary_only":
        return [
            {"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"type": "agentMessage", "id": "m0", "text": "Let me think about the reply...",
                         "phase": "commentary"}}},
            {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
                "id": turn_id, "items": [], "status": "completed"}}},
        ]
    if SCENARIO == "late_commentary":
        return [
            {"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"type": "agentMessage", "id": "m1", "text": "FINAL", "phase": "final_answer"}}},
            {"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"type": "agentMessage", "id": "m2", "text": "late note", "phase": "commentary"}}},
            {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
                "id": turn_id, "items": [], "status": "completed"}}},
        ]
    if SCENARIO == "unphased":
        return [
            {"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                "item": {"type": "agentMessage", "id": "m1", "text": "LEGACY", "phase": None}}},
            {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
                "id": turn_id, "items": [], "status": "completed"}}},
        ]
    other_bucket = {"rateLimits": {"limitId": "codex_other", "normalModelSlug": "gpt-other",
                                   "primary": {"usedPercent": 99, "windowDurationMins": 300}}}
    ev += [
        {"method": "rawResponseItem/completed", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"type": "message", "role": "assistant"}}},
        {"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"type": "agentMessage", "id": "m0", "text": "thinking...", "phase": "commentary"}}},
        {"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
            "item": {"type": "agentMessage", "id": "m1",
                     "text": answer_for(prompt) if SCENARIO == "smart" else "{\"intent\": \"interested\"}",
                     "phase": "final_answer"}}},
        {"method": "rawResponse/completed", "params": {"threadId": thread_id, "turnId": turn_id}},
        {"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn_id,
            # Two model responses this turn: `last` is only the second one.
            "tokenUsage": {"last": {"inputTokens": 5, "cachedInputTokens": 1,
                                    "cacheWriteInputTokens": 0, "outputTokens": 1,
                                    "reasoningOutputTokens": 0, "totalTokens": 6},
                           "total": {"inputTokens": 1200, "cachedInputTokens": 200,
                                     "outputTokens": 40, "reasoningOutputTokens": 10,
                                     "totalTokens": 1240}}}},
        {"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1791910000}}}},
        # Another model's bucket must never be reported as this turn's.
        {"method": "account/rateLimits/updated", "params": other_bucket},
        {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {
            "id": turn_id, "items": [], "status": "completed", "durationMs": 850}}},
    ]
    return ev


for line in sys.stdin:
    msg = json.loads(line)
    method, params, mid = msg.get("method"), msg.get("params"), msg.get("id")
    if method is None:  # a response to one of our server requests
        log({"response_to_server_request": msg})
        continue
    log({"method": method, "params": params})
    if mid is None:
        continue
    if SCENARIO == "crash" and method == "config/read":
        sys.exit(3)

    result = {}
    if method == "initialize":
        agent_version = "0.160.0" if SCENARIO == "server_version_mismatch" else VERSION
        result = {"userAgent": f"sellbird/{agent_version} (fake)",
                  "codexHome": os.environ.get("CODEX_HOME")}
        send({"method": "remoteControl/status/changed", "params": {"status": "disabled"}})
    elif method == "account/read":
        account = {"type": "chatgpt", "email": "x@example.com", "planType": "plus"}
        if SCENARIO == "apikey":
            account = {"type": "apiKey"}
        if SCENARIO == "logged_out" or SNAPSHOT is None:
            account = None
        result = {"account": account, "requiresOpenaiAuth": True}
    elif method == "account/rateLimits/read":
        general = {"limitId": "codex",
                   "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": 1791910000},
                   "secondary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1792400000}}
        if SCENARIO == "denied_no_windows":
            general = {"limitId": "codex", "primary": None, "secondary": None,
                       "rateLimitReachedType": "rate_limit_reached"}
        result = {"rateLimits": general, "rateLimitsByLimitId": {
            "codex": general,
            "codex_other": {"limitId": "codex_other", "normalModelSlug": "gpt-other",
                            "primary": {"usedPercent": 99, "windowDurationMins": 300}}},
            "ordinaryUsageAllowed": SCENARIO != "exhausted"}
    elif method == "config/read":
        result = {"config": effective_config(), "origins": {}}
    elif method == "thread/start":
        result = {
            "thread": {"id": THREAD_ID}, "model": params.get("model") or "gpt-6.1-sol",
            "modelProvider": "openai", "approvalPolicy": "never",
            "sandbox": {"type": "readOnly", "networkAccess": False},
            "instructionSources": ["/home/u/AGENTS.md"] if SCENARIO == "instructions" else [],
            "cwd": params.get("cwd"), "approvalsReviewer": "user",
        }
        if SCENARIO == "wrong_model":
            result["model"] = "gpt-other"
    elif method == "mcpServerStatus/list":
        data = []
        if SCENARIO == "mcp_tools":
            data = [{"name": "inherited", "tools": {"pwn": {"name": "pwn", "inputSchema": {}}},
                     "serverInfo": {"name": "inherited"}, "authStatus": "unsupported",
                     "resources": [], "resourceTemplates": []}]
        if SCENARIO == "mcp_disabled":
            data = [{"name": "inherited", "tools": {}, "serverInfo": None,
                     "authStatus": "unsupported", "resources": [], "resourceTemplates": []}]
        result = {"data": data, "nextCursor": None}
    elif method == "hooks/list":
        hooks = [{"handlerType": "command", "command": "curl evil"}] if SCENARIO == "hooks" else []
        result = {"data": [{"cwd": params["cwds"][0], "hooks": hooks, "warnings": [], "errors": []}]}
        if SCENARIO == "stall_stdin":
            # Stop reading: a large turn/start write now blocks on a full pipe.
            send({"id": mid, "result": result})
            time.sleep(3600)
    elif method == "turn/start":
        prompt = "".join(i.get("text", "") for i in params.get("input", []) if isinstance(i, dict))
        # Turn-level faults, chosen per call by a marker in the prompt.
        if "FAULT:crash_turn" in prompt:
            sys.exit(5)
        if "FAULT:partial" in prompt:
            sys.stdout.write('{"id": ' + str(mid) + ', "result": {"tu')
            sys.stdout.flush()
            sys.exit(0)
        if "FAULT:malformed" in prompt:
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            time.sleep(3600)
        if "FAULT:oversized" in prompt:
            sys.stdout.write('{"method": "x", "params": "' + "A" * (9 * 1024 * 1024) + '"}\n')
            sys.stdout.flush()
            time.sleep(3600)
        if "FAULT:hang" in prompt:
            time.sleep(3600)
        send({"id": mid, "result": {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}}})
        if "FAULT:usage_limit" in prompt:
            send({"method": "turn/completed", "params": {"threadId": THREAD_ID, "turn": {
                "id": "turn-1", "items": [], "status": "failed",
                "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"}}}})
            continue
        if "FAULT:toolcall" in prompt:
            send({"method": "rawResponseItem/completed", "params": {"threadId": THREAD_ID,
                  "turnId": "turn-1", "item": {"type": "function_call", "name": "exec_command"}}})
            time.sleep(3600)
        log({"prompt_chars": len(prompt), "prompt_has_non_ascii": any(ord(c) > 127 for c in prompt),
             "prompt_sha": __import__("hashlib").sha256(prompt.encode()).hexdigest()})
        for ev in turn_events(THREAD_ID, "turn-1", prompt):
            send(ev)
        if SCENARIO == "hang":
            time.sleep(3600)
        continue
    send({"id": mid, "result": result})
