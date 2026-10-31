"""Dashboard API for the "Other mail" tab (job-search mode, local fork).

Mounted by dashboard.py, so the same token guard covers it."""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harvey import job_search

router = APIRouter()

STATUSES = ("new", "follow", "ignored", "linked")


def _state():
    from harvey.dashboard import _state as dashboard_state
    return dashboard_state()


async def _rows(state, sql: str, params: tuple = ()) -> list[dict]:
    async with state._connect() as db:
        await job_search.ensure_table(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


@router.get("/api/other-mail")
async def list_other_mail(noise: int = 0):
    state = _state()
    rows = await _rows(state, "SELECT * FROM other_mail WHERE noise <= ? "
                       "ORDER BY received_at DESC LIMIT 300", (1 if noise else 0,))
    hidden = (await _rows(state, "SELECT COUNT(*) n FROM other_mail WHERE noise = 1"))[0]["n"]
    apps = [{"url": a["url"], "empresa": a["empresa"], "titulo": a["titulo"],
             "fecha": a["fecha"], "respuesta": a["respuesta"]}
            for a in job_search.sent_applications()]
    return {"mail": rows, "applications": apps, "noise_hidden": hidden}


@router.post("/api/other-mail/import")
async def import_other_mail(request: Request):
    """Read past mail (default 30 days) that arrived before this tab existed."""
    try:
        days = int((await request.json()).get("days") or 30)
    except Exception:
        days = 30
    from harvey.config import load_config, load_env
    from harvey.integrations.mail_provider import get_mail_provider
    provider = get_mail_provider(load_config(), load_env())
    if provider is None or not provider.is_configured():
        return JSONResponse({"success": False, "message": "Gmail isn't connected."}, status_code=400)
    try:
        kept = await job_search.import_recent(_state(), provider, days=max(1, min(days, 90)))
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Import failed: {e}"}, status_code=502)
    return {"success": True, "kept": kept}


@router.post("/api/other-mail/{mail_id}")
async def update_other_mail(mail_id: str, request: Request):
    """action: follow | ignore | new | link (with app_url and respuesta)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    action = data.get("action") if isinstance(data, dict) else None
    state = _state()
    found = await _rows(state, "SELECT * FROM other_mail WHERE id = ?", (mail_id,))
    if not found:
        return JSONResponse({"success": False, "message": "No such email."}, status_code=404)
    mail = found[0]
    fields: dict = {}
    if action in ("follow", "ignore", "new"):
        fields["status"] = {"follow": "follow", "ignore": "ignored", "new": "new"}[action]
    elif action == "link":
        app_url = str(data.get("app_url") or "")
        respuesta = str(data.get("respuesta") or "")
        if not app_url:
            return JSONResponse({"success": False, "message": "Pick an application."}, status_code=400)
        # "acuse" (they got it) links the email without changing the outcome.
        if respuesta and respuesta != "acuse":
            try:
                ok = job_search.record_outcome(app_url, respuesta, nota=mail["subject"])
            except ValueError as e:
                return JSONResponse({"success": False, "message": str(e)}, status_code=400)
            if not ok:
                return JSONResponse({"success": False, "message": "That application isn't in the log."},
                                    status_code=404)
        fields.update(status="linked", application_url=app_url)
    else:
        return JSONResponse({"success": False, "message": "Unknown action."}, status_code=400)
    sets = ", ".join(f"{k} = ?" for k in fields)
    async with state._connect() as db:
        await db.execute(f"UPDATE other_mail SET {sets} WHERE id = ?", (*fields.values(), mail_id))
        await db.commit()
    await state.log_action("other_mail", "dashboard", {"id": mail_id, **fields})
    return {"success": True, **fields}
