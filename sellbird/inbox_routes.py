"""The dashboard's Inboxes routes (sellbird.mailboxes).

Reading every inbox and pausing one are open to the coding agent
(sellbird.agent_policy); resuming, caps, names, removing and choosing a
campaign's inboxes are the user's, and so is connecting one: a Gmail or
Microsoft sign-in opens in the browser on the computer running Sellbird
(the same flow as `sellbird inboxes connect`), and an SMTP password goes
straight to the system keychain. None of these routes is on the agent's
allowlist. No response here carries a credential, a keychain ref or SMTP
settings: rows are built from explicit fields.

The main mailbox has no row; in a path it is `main`.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sellbird.mailboxes import (
    HARD_MAX_INBOX_CAP, MAIN, ConnectRefused, MailboxStore, build_provider,
    check_provider_allows_inboxes, connect_oauth_mailbox, connect_smtp_mailbox, load_inboxes,
)
from sellbird.secrets import SecretStore, SecretStoreUnavailable

logger = logging.getLogger("sellbird.inbox_routes")

router = APIRouter()
MAIN_PATH = "main"


def _refuse(message: str, code: int = 400):
    return JSONResponse({"success": False, "message": message}, status_code=code)


def _store() -> MailboxStore:
    from sellbird import dashboard
    return MailboxStore(db_path=str(dashboard.DB_PATH))


def _secrets() -> SecretStore:
    return SecretStore()


# How to connect the main mailbox, by provider, when it isn't connected.
MAIN_CONNECT_HINTS = {
    "gmail": "Sign in with Google below as this address (or run `sellbird gmail auth`).",
    "microsoft": "Sign in with Microsoft below as this address (or run `sellbird microsoft auth`).",
    "smtp": "Fill in the SMTP settings under channels.email in sellbird.yaml and the "
            "password in .env, then run `sellbird doctor`.",
}


def _config_env():
    from sellbird.config import load_config, load_env
    return load_config(), load_env()


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _row(store: MailboxStore, mailbox_id: str) -> dict | None:
    row = await store.get_mailbox(mailbox_id)
    return row if row and row.get("state") != "retired" else None


@router.get("/api/inboxes")
async def list_inboxes():
    from sellbird.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider

    store = _store()
    await store.init_db()
    config, env = _config_env()
    email = config.channels.email
    native = (email.provider or "").strip().lower() in NATIVE_PROVIDERS
    main = get_mail_provider(config, env) if native else None
    inboxes = await load_inboxes(store, config, env, main)
    rows = {r["id"]: r for r in await store.list_mailboxes()}
    out = []
    for inbox in inboxes.inboxes.values():
        row = rows.get(inbox.id, {})
        if not inbox.connected:
            state = "not_connected"
        elif inbox.paused_reason:
            state = "paused"
        else:
            state = "sending"
        out.append({
            "id": MAIN_PATH if inbox.main else inbox.id,
            "main": inbox.main,
            "address": inbox.address,
            "name": inbox.name,
            "connection": "main" if inbox.main else row.get("connection", ""),
            "provider": email.provider if inbox.main else row.get("provider", ""),
            "state": state,
            "paused_reason": inbox.paused_reason,
            "cold_cap": inbox.cap,
            "cap_is_yours": row.get("cap_override") is not None,
            "cold_sent_today": inbox.sent_today,
            "first_emails_waiting": max(0, inbox.queued_first),
            "warmup_started_at": row.get("warmup_started_at") or "",
            "connect_hint": (MAIN_CONNECT_HINTS.get((email.provider or "").strip().lower(), "")
                             if inbox.main and not inbox.connected else ""),
        })
    paused = await store.get_setting("sending_paused") or ""
    return {
        "inboxes": out,
        "several": inboxes.several,
        "native": native,
        "max_daily_sends": email.max_daily_sends,
        "per_inbox_daily_cap": getattr(email, "per_inbox_daily_cap", 30),
        "pools": {cid: sorted(MAIN_PATH if m == MAIN else m for m in ids)
                  for cid, ids in (await store.campaign_pools()).items()},
        "campaigns": await store._rows(
            "SELECT id, name, status FROM campaigns ORDER BY created_at DESC LIMIT 200"),
        "sending_paused": paused,
        "connect_command": "sellbird inboxes connect gmail|microsoft|smtp --address you@domain",
        "can_connect": _can_connect(env),
        "connect_problems": _connect_problems(env),
        "connecting": _connect_job_view(),
    }


@router.post("/api/inboxes/{mailbox_id}/pause")
async def pause_inbox(mailbox_id: str, request: Request):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id. The main mailbox pauses with all "
                       "sending, on the Outbox tab.", 404)
    principal = getattr(request.state, "principal", "user")
    reason = ((await _body(request)).get("reason") or "").strip()[:200]
    reason = reason or ("paused by your coding agent" if principal == "agent" else "paused by you")
    await store.set_pause(row["id"], reason)
    await store.log_action(action_type="inbox_paused", agent="agent" if principal == "agent" else "you",
                           details={"inbox": row["address"], "reason": reason})
    return {"success": True, "paused": True, "address": row["address"]}


@router.post("/api/inboxes/{mailbox_id}/resume")
async def resume_inbox(mailbox_id: str):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    await store.set_pause(row["id"], "")
    await store.log_action(action_type="inbox_resumed", agent="you",
                           details={"inbox": row["address"]})
    return {"success": True}


@router.post("/api/inboxes/{mailbox_id}/cap")
async def set_inbox_cap(mailbox_id: str, request: Request):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id. The main mailbox's cap is "
                       "channels.email.per_inbox_daily_cap in sellbird.yaml.", 404)
    cap = (await _body(request)).get("cap")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int)
                            or not 1 <= cap <= HARD_MAX_INBOX_CAP):
        return _refuse(f"A cap is a whole number from 1 to {HARD_MAX_INBOX_CAP}, "
                       "or null for the default.")
    await store.set_cap(row["id"], cap)
    await store.log_action(action_type="inbox_cap", agent="you",
                           details={"inbox": row["address"], "cap": cap})
    return {"success": True}


@router.post("/api/inboxes/{mailbox_id}/name")
async def set_inbox_name(mailbox_id: str, request: Request):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    name = (await _body(request)).get("name")
    if not isinstance(name, str) or len(name) > 100 or any(c in name for c in "\r\n<>\""):
        return _refuse("A name is up to 100 characters, on one line, without < > or \".")
    await store.set_display_name(row["id"], name)
    return {"success": True}


@router.post("/api/inboxes/{mailbox_id}/remove")
async def remove_inbox(mailbox_id: str, request: Request):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    if (await _body(request)).get("confirm") != row["address"]:
        return _refuse("Type the inbox's address to confirm: threads it started end.")
    await store.retire(row["id"])
    await store.log_action(action_type="inbox_removed", agent="you",
                           details={"inbox": row["address"]})
    return {"success": True}


@router.post("/api/campaigns/{campaign_id}/inboxes")
async def set_campaign_inboxes(campaign_id: str, request: Request):
    store = _store()
    await store.init_db()
    if not await store._rows("SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,)):
        return _refuse("No campaign with that id.", 404)
    ids = (await _body(request)).get("inboxes")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return _refuse('Expected {"inboxes": ["main", "<inbox id>", ...]} ([] = every inbox).')
    chosen = []
    for i in ids:
        if i == MAIN_PATH:
            chosen.append(MAIN)
        elif await _row(store, i) is not None:
            chosen.append(i)
        else:
            return _refuse(f"No connected inbox {i!r}.", 404)
    await store.set_campaign_pool(campaign_id, chosen)
    return {"success": True}


# ── Connecting an inbox from the dashboard ──
#
# A Gmail or Microsoft sign-in blocks for up to five minutes while the user
# finishes it in the browser, so it runs as one background job; the page
# polls /api/inboxes/connect/status. One sign-in at a time: both flows
# listen on a fixed local port.

_connect_job: dict = {"state": "idle"}
_connect_task: asyncio.Task | None = None


# A real Google OAuth client id: digits, a dash, then .apps.googleusercontent.com.
_GOOGLE_CLIENT_ID = re.compile(r"^\d+-[0-9a-z]+\.apps\.googleusercontent\.com$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _connect_problem(kind: str, env) -> str:
    """Why a browser sign-in can't start yet, or "" when it can. Checked
    before the browser opens, so a missing or mistyped client never sends the
    user to a provider error page."""
    if kind == "gmail":
        client_id = (getattr(env, "gmail_client_id", "") or "").strip()
        if not client_id or not (getattr(env, "gmail_client_secret", "") or "").strip():
            return ("Google sign-in isn't set up yet. Sellbird signs in to Gmail through "
                    "your own Google OAuth client, so it needs its Client ID and secret first.")
        if not _GOOGLE_CLIENT_ID.match(client_id):
            return ("The Google Client ID in Settings isn't a real one. It looks like "
                    "123456789-abc123.apps.googleusercontent.com.")
        return ""
    if kind == "microsoft":
        client_id = (getattr(env, "m365_client_id", "") or "").strip()
        if not client_id:
            return ("Microsoft sign-in isn't set up yet. Sellbird signs in through your own "
                    "Microsoft app registration, so it needs its Application (client) ID first.")
        if not _GUID.match(client_id):
            return ("The Microsoft Application (client) ID in Settings isn't a real one. It "
                    "looks like 1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901.")
        return ""
    return ""


def _can_connect(env) -> dict:
    return {k: not _connect_problem(k, env) for k in ("gmail", "microsoft")}


def _connect_problems(env) -> dict:
    return {k: _connect_problem(k, env) for k in ("gmail", "microsoft")}


def _connect_job_view() -> dict:
    return {k: _connect_job.get(k, "") for k in ("state", "kind", "address", "message")}


async def _test(row: dict, config, env, secrets) -> tuple[bool, str]:
    provider = build_provider(row, config=config, env=env, secrets=secrets)
    if provider is None:
        return False, "Couldn't build a connection to test."
    try:
        return await asyncio.wait_for(provider.test_connection(), timeout=30)
    except Exception as e:  # a network failure must not 500 the page
        return False, f"Couldn't check the connection ({type(e).__name__})."


def _sign_in(kind: str, address: str, env, main: bool = False) -> dict | None:
    """The browser sign-in, in a worker thread. An extra inbox's token stays in
    memory for the keychain; the main mailbox's goes to its usual token file,
    exactly as `sellbird gmail auth` / `sellbird microsoft auth` store it."""
    captured: dict = {}
    save = None if main else captured.update
    expected = address or None
    if kind == "gmail":
        from sellbird.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret,
                           save=save, expected_address=expected)
    else:
        from sellbird.integrations.microsoft import run_auth_flow
        ok = run_auth_flow(env.m365_client_id, env.m365_tenant_id,
                           save=save, expected_address=expected)
    if main:
        return {"main": True} if ok else None
    return captured if ok and captured else None


async def _run_main_connect(kind: str, address: str) -> None:
    config, env = _config_env()
    try:
        done = await asyncio.to_thread(_sign_in, kind, address, env, True)
        if done is None:
            _connect_job.update(state="failed", message=(
                "The sign-in didn't finish, or you signed in as a different address. "
                "Nothing changed."))
            return
        store = _store()
        await store.init_db()
        await store.log_action(action_type="inbox_connected", agent="you",
                               details={"inbox": address or "main mailbox", "provider": kind})
        _connect_job.update(state="connected", message="")
    except Exception:
        logger.exception("Connecting the main mailbox failed")
        _connect_job.update(state="failed", message="Connecting failed. See the dashboard log.")


async def _run_oauth_connect(kind: str, address: str, name: str, warmup: str) -> None:
    config, env = _config_env()
    try:
        token = await asyncio.to_thread(_sign_in, kind, address, env)
        if token is None:
            _connect_job.update(state="failed", message=(
                "The sign-in didn't finish, or you signed in as a different address. "
                "Nothing was connected."))
            return
        store, secrets = _store(), _secrets()
        await store.init_db()
        mailbox_id = await connect_oauth_mailbox(
            store, secrets, config, provider="google" if kind == "gmail" else "microsoft",
            address=address, display_name=name, warmup_started_at=warmup, token=token)
        await store.log_action(action_type="inbox_connected", agent="you",
                               details={"inbox": address, "provider": kind})
        ok, detail = await _test(await store.get_mailbox(mailbox_id), config, env, secrets)
        _connect_job.update(state="connected", message=detail if not ok else "")
    except (ConnectRefused, SecretStoreUnavailable) as e:
        _connect_job.update(state="failed", message=str(e))
    except Exception:
        logger.exception("Connecting an inbox failed")
        _connect_job.update(state="failed", message="Connecting failed. See the dashboard log.")


def _connect_fields(body: dict) -> tuple[str, str, str]:
    address = body.get("address")
    name = body.get("name") or ""
    warmup = body.get("warmup_started") or ""
    if not isinstance(address, str) or "@" not in address or len(address) > 254:
        raise ConnectRefused("Enter the inbox's full email address.")
    if not isinstance(name, str) or len(name) > 100 or any(c in name for c in "\r\n<>\""):
        raise ConnectRefused("A name is up to 100 characters, on one line, without < > or \".")
    if not isinstance(warmup, str):
        raise ConnectRefused("Give the warmup start as YYYY-MM-DD.")
    return address.strip().lower(), name.strip(), warmup.strip()


@router.post("/api/inboxes/main/connect")
async def connect_main_mailbox():
    """Sign the main mailbox in from the dashboard: the same browser sign-in
    as `sellbird gmail auth` / `sellbird microsoft auth`."""
    global _connect_task
    config, env = _config_env()
    provider = (config.channels.email.provider or "").strip().lower()
    if provider not in ("gmail", "microsoft"):
        return _refuse("The main mailbox signs in with a browser only for Gmail or "
                       "Microsoft 365. Set channels.email.provider in sellbird.yaml.")
    problem = _connect_problem(provider, env)
    if problem:
        return _refuse(problem)
    if _connect_task is not None and not _connect_task.done():
        return _refuse("A sign-in is already open. Finish it in your browser, or wait "
                       "five minutes for it to time out.", 409)
    address = (getattr(config.persona, "email", "") or "").strip().lower()
    _connect_job.clear()
    _connect_job.update(state="waiting", kind=provider, address=address, message="")
    _connect_task = asyncio.create_task(_run_main_connect(provider, address))
    return {"success": True, "started": True, "kind": provider}


@router.post("/api/inboxes/connect/{kind}")
async def connect_inbox(kind: str, request: Request):
    global _connect_task
    if kind not in ("gmail", "microsoft", "smtp"):
        return _refuse("Connect gmail, microsoft or smtp.", 404)
    body = await _body(request)
    config, env = _config_env()
    try:
        check_provider_allows_inboxes(config)
        address, name, warmup = _connect_fields(body)
    except ConnectRefused as e:
        return _refuse(str(e))

    if kind == "smtp":
        return await _connect_smtp(body, address, name, warmup, config, env)

    problem = _connect_problem(kind, env)
    if problem:
        return _refuse(problem)
    if _connect_task is not None and not _connect_task.done():
        return _refuse("A sign-in is already open. Finish it in your browser, or wait "
                       "five minutes for it to time out.", 409)
    _connect_job.clear()
    _connect_job.update(state="waiting", kind=kind, address=address, message="")
    _connect_task = asyncio.create_task(_run_oauth_connect(kind, address, name, warmup))
    return {"success": True, "started": True}


@router.get("/api/inboxes/connect/status")
async def connect_status():
    return _connect_job_view()


async def _connect_smtp(body, address, name, warmup, config, env):
    from sellbird.integrations.smtp_mail import SmtpSettings

    password = body.get("password")
    host = body.get("smtp_host")
    imap_host = body.get("imap_host") or ""
    username = body.get("username") or ""
    ports = (body.get("smtp_port", 587), body.get("imap_port", 993))
    if not isinstance(password, str) or not password or len(password) > 1024:
        return _refuse("Enter the inbox's SMTP password (or app password).")
    if not isinstance(host, str) or not host.strip() or not isinstance(imap_host, str) \
            or not isinstance(username, str):
        return _refuse("Enter the SMTP server's host name.")
    if any(isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535 for p in ports):
        return _refuse("A port is a whole number from 1 to 65535.")
    settings = SmtpSettings(host=host.strip(), username=username.strip() or address,
                            password=password, port=ports[0],
                            imap_host=imap_host.strip(), imap_port=ports[1])
    store, secrets = _store(), _secrets()
    await store.init_db()
    try:
        mailbox_id = await connect_smtp_mailbox(store, secrets, config, address=address,
                                                display_name=name, warmup_started_at=warmup,
                                                settings=settings)
    except (ConnectRefused, SecretStoreUnavailable) as e:
        return _refuse(str(e))
    await store.log_action(action_type="inbox_connected", agent="you",
                           details={"inbox": address, "provider": "smtp"})
    ok, detail = await _test(await store.get_mailbox(mailbox_id), config, env, secrets)
    return {"success": True, "address": address, "test_ok": ok, "test_detail": detail}


@router.post("/api/inboxes/{mailbox_id}/test")
async def test_inbox(mailbox_id: str):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    config, env = _config_env()
    ok, detail = await _test(row, config, env, _secrets())
    return {"success": True, "ok": ok, "detail": detail}
