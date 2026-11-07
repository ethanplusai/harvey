"""The dashboard's Inboxes routes (sellbird.mailboxes).

Reading every inbox and pausing one are open to the coding agent
(sellbird.agent_policy); resuming, caps, names, removing and choosing a
campaign's inboxes are the user's. Connecting an inbox needs a browser
sign-in or a password prompt, so it stays on the command line
(`sellbird inboxes connect`). No response here carries a credential, a
keychain ref or SMTP settings: rows are built from explicit fields.

The main mailbox has no row; in a path it is `main`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sellbird.mailboxes import HARD_MAX_INBOX_CAP, MAIN, MailboxStore, load_inboxes

router = APIRouter()
MAIN_PATH = "main"


def _refuse(message: str, code: int = 400):
    return JSONResponse({"success": False, "message": message}, status_code=code)


def _store() -> MailboxStore:
    from sellbird import dashboard
    return MailboxStore(db_path=str(dashboard.DB_PATH))


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
