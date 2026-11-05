"""The company register: every company Harvey has found, one row each.

Built on read from the database, so nothing is lost and it is always current:
where the company came from, why it fits, what Harvey has done about it, and
— when nothing was sent — why not. Companies with no contact found stay in
the register for people to follow up another way.
"""

from __future__ import annotations

import csv
import io

from harvey.config import ComplianceConfig, live_do_not_contact
from harvey.fit import company_fit, current_roles
from harvey.gate import do_not_contact_match

COLUMNS = [
    "company", "website", "sector", "area", "fit", "why_selected",
    "status", "why_not_contacted", "contacts", "contact_form", "hiring",
    "found_via", "first_found",
]
LIVE = {"new", "queued"}
DONE = {"contacted", "replied", "meeting", "closed"}


def _contacts(prospects: list[dict]) -> str:
    out = []
    for p in prospects:
        name = f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip() or "General inbox"
        title = p.get("title") or ""
        email = p.get("email") or "no email"
        status = p.get("email_status") or "unchecked"
        label = f"{name}" + (f" ({title})" if title and title != "General inbox" else "")
        flag = " [not relevant]" if p.get("status") == "lost" else ""
        out.append(f"{label}: {email} [{status}]{flag}")
    return "; ".join(out)


def company_status(company: dict, prospects: list[dict], outbox: list[dict],
                   observations: list[dict], dnc: list[str],
                   send_to_risky: bool = False) -> tuple[str, str]:
    """(status, why) for one company. ``why`` says why nothing went out, or
    what happens next. ``send_to_risky`` is channels.email.send_to_risky."""
    domain = (company.get("domain") or "").lower()
    if domain and dnc and do_not_contact_match(f"x@{domain}", dnc):
        return "Excluded", "On your do-not-contact list"
    listed = [p for p in prospects
              if dnc and p.get("email") and do_not_contact_match(p["email"], dnc)]
    if listed and len(listed) == len([p for p in prospects if p.get("email")]):
        return "Excluded", "Every contact here is on your do-not-contact list"
    prospects = [p for p in prospects if p not in listed]
    if any(p.get("status") == "opted_out" for p in prospects):
        return "Opted out", "A contact here asked not to be emailed"

    states = {o.get("status") for o in outbox}
    if "sent" in states or any(p.get("status") in DONE for p in prospects):
        replied = any(p.get("status") in {"replied", "meeting", "closed"} for p in prospects)
        return ("Replied" if replied else "Emailed"), ""
    if states & {"approved", "sending"}:
        when = min((o.get("send_at") or "" for o in outbox if o.get("status") == "approved"), default="")
        return "Approved", f"Approved by you; first send due {when[:10]}" if when else "Approved by you"
    if "pending_review" in states:
        return "Draft waiting", "Email written, waiting for your review in the Outbox"

    # The Writer picks up only 'new' contacts. A 'queued' one has had its
    # drafts staged; with nothing pending (above), they were stopped.
    if any(p.get("status") == "queued" for p in prospects) and \
            not any(p.get("status") == "new" for p in prospects):
        if states & {"rejected", "failed", "cancelled"}:
            return "Not contacted", "Drafts were rejected or blocked before sending"
        return "Queued", "Staged for a campaign"
    if any(p.get("status") == "write_failed" for p in prospects) and \
            not any(p.get("status") in LIVE for p in prospects):
        return "Not contacted", ("Harvey couldn't write a draft after several tries; "
                                 "see the log, and fix the cause to retry")
    live = [p for p in prospects if p.get("status") in LIVE]
    statuses = {(p.get("email_status") or "") for p in live}
    has_form = any(o.get("signal_code") == "CONTACT_FORM_URL" for o in observations)
    form = " Contact form available." if has_form else ""
    if "verified" in statuses or (send_to_risky and "risky" in statuses):
        return "Ready", "Deliverable email found; Harvey will write to it on a coming cycle"
    if "risky" in statuses:
        return "Not contacted", ("Only a risky (catch-all) email, which your rules don't send to. "
                                 "Try phone, LinkedIn or the cold-email domain later." + form)
    if live and statuses <= {"guess", "invalid", ""}:
        who = ", ".join(sorted({(p.get("title") or "contact") for p in live}))[:120]
        return "Not contacted", (f"Found {who}, but no valid email (address guessed or rejected by "
                                 "the verifier). Try phone, LinkedIn or a contact database." + form)
    if prospects and not live:
        if states & {"rejected", "failed", "cancelled"}:
            return "Not contacted", "Drafts were rejected or blocked before sending"
        return "Not contacted", "Contacts found were not relevant (e.g. not a factory, wrong person)"
    if company.get("prospects_checked_at"):
        return "Not contacted", "Website read, but no named person or usable email on it." + form
    if company.get("profile_checked_at"):
        return "Not processed yet", "Website read; Harvey hasn't looked for contacts here yet." + form
    return "Not processed yet", "Found by search; website not read yet"


async def build_register(query_db, config, company_id: str | None = None) -> list[dict]:
    """Every company (or just ``company_id``), best fit first, as register rows."""
    one, args = ("", ()) if company_id is None else (" AND {col} = ?", (company_id,))
    companies = await query_db(
        "SELECT * FROM companies WHERE 1=1" + one.format(col="id") + " ORDER BY created_at", args)
    if not companies:
        return []
    prospects = await query_db(
        "SELECT company_id, first_name, last_name, title, email, email_status, status "
        "FROM prospects WHERE 1=1" + one.format(col="company_id"), args)
    outbox = await query_db(
        "SELECT p.company_id, o.status, o.send_at FROM outbox o JOIN prospects p ON p.id = o.prospect_id "
        "WHERE 1=1" + one.format(col="p.company_id"), args)
    observations = await query_db(
        "SELECT company_id, signal_code, value_text, value_num, observed_at FROM observations "
        "WHERE signal_code IN "
        "('DECISION_MAKER_TITLE', 'CONTACT_FORM_URL', 'HIRING_ROLE')" + one.format(col="company_id"), args)
    by_p: dict[str, list] = {}
    by_o: dict[str, list] = {}
    by_obs: dict[str, list] = {}
    for rows, by in ((prospects, by_p), (outbox, by_o), (observations, by_obs)):
        for r in rows:
            by.setdefault(r["company_id"], []).append(r)

    dnc = _do_not_contact(config)
    channels = getattr(config, "channels", None)
    send_to_risky = bool(getattr(getattr(channels, "email", None), "send_to_risky", False))
    rows = []
    for c in companies:
        cid = c["id"]
        ps, obs = by_p.get(cid, []), by_obs.get(cid, [])
        fit, reasons = company_fit(c, ps, obs, config, dnc)
        status, why = company_status(c, ps, by_o.get(cid, []), obs, dnc, send_to_risky)
        sector, area = c.get("industry") or "", c.get("location") or ""
        found = f"Found via {c['source']}" if c.get("source") else ""
        rows.append({
            # id and fit_reasons are for the dashboard; the CSV leaves them out.
            "id": cid,
            "fit_reasons": reasons,
            "company": c.get("name") or c.get("domain") or "",
            "website": c.get("website") or (f"https://{c['domain']}" if c.get("domain") else ""),
            "sector": sector,
            "area": area,
            "fit": fit,
            "why_selected": "; ".join(x for x in (found, " · ".join(reasons)) if x),
            "status": status,
            "why_not_contacted": why,
            "contacts": _contacts(ps),
            "contact_form": next((o.get("value_text") or "" for o in obs
                                  if o.get("signal_code") == "CONTACT_FORM_URL"), ""),
            "hiring": "; ".join(dict.fromkeys(
                (o.get("value_text") or "")[:60] for o in current_roles(obs)
                if o.get("value_text")))[:300],
            "found_via": c.get("source_url") or "",
            "first_found": (c.get("created_at") or "")[:10],
        })
    rows.sort(key=lambda r: r["fit"], reverse=True)
    return rows


def _do_not_contact(config) -> list[str]:
    """The do-not-contact list as the config file says it is now, as the
    sender reads it, so an entry added while Harvey runs shows at once."""
    compliance = getattr(config, "compliance", None)
    if isinstance(compliance, ComplianceConfig):
        try:
            return list(live_do_not_contact(config) or [])
        except Exception:
            pass
    return list(getattr(compliance, "do_not_contact", None) or [])


def sqlite_query(db_path):
    """A ``query_db`` for build_register that reads ``db_path`` directly
    (the CLI has no dashboard to borrow one from)."""
    import aiosqlite

    async def query_db(sql: str, params: tuple = ()) -> list[dict]:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    return query_db


def _cell(value):
    """Text from websites must not run as a spreadsheet formula."""
    if isinstance(value, (int, float)):
        return value
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in COLUMNS})
    return buf.getvalue()
