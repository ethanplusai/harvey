"""Job-search mode (local fork, not upstream Harvey): mail from outside the
prospect list.

Upstream, the handler drops any inbound email whose sender isn't a prospect.
In job-search mode those are often the answers that matter most: an ATS
saying "unfortunately", a recruiter asking for availability, a take-home
test. They are kept in their own table (`other_mail`, created here rather
than in state.MIGRATIONS so a future upstream migration can't collide with
it), matched to an application in data/aplicaciones.csv when the sender or
the text names its company, and given a suggested outcome from keywords.

Nothing is written to the applications log until the user confirms it on
the dashboard's "Otros correos" tab.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("harvey.job_search")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRO = PROJECT_ROOT / "data" / "aplicaciones.csv"
CAMPOS_REGISTRO = ["fecha", "estado", "empresa", "titulo", "url", "llenos", "pendientes",
                   "peso", "respuesta", "fecha_respuesta", "nota"]
# What the applications log records as an outcome, worst to best.
RESPUESTAS = ["nada", "rechazo", "prueba", "entrevista", "oferta"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS other_mail (
    id TEXT PRIMARY KEY,                 -- provider message id
    from_email TEXT NOT NULL,
    subject TEXT DEFAULT '',
    snippet TEXT DEFAULT '',
    received_at TEXT DEFAULT '',         -- UTC, "YYYY-MM-DD HH:MM:SS"
    thread_ref TEXT DEFAULT '',
    noise INTEGER DEFAULT 0,             -- 1 = account/newsletter mail, hidden by default
    is_ats INTEGER DEFAULT 0,            -- sent through a hiring system
    application_url TEXT DEFAULT '',     -- matched (or confirmed) row of aplicaciones.csv
    suggestion TEXT DEFAULT '',          -- rechazo | prueba | entrevista | acuse | alerta | ''
    status TEXT DEFAULT 'new',           -- new | follow | ignored | linked
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_other_mail_status ON other_mail(status, received_at);
"""

# Hiring systems send from their own domains, usually from a no-reply address.
ATS_DOMAINS = (
    "ashbyhq.com", "greenhouse.io", "greenhouse-mail.io", "lever.co", "workablemail.com",
    "workable.com", "smartrecruiters.com", "bamboohr.com", "breezy.hr", "recruitee.com",
    "teamtailor.com", "teamtailor-mail.com", "jobvite.com", "myworkday.com", "workday.com",
    "icims.com", "personio.de", "personio.com", "getonbrd.com", "himalayas.app",
    "pinpointhq.com", "pinpoint.email", "rippling.com", "jazzhr.com", "applytojob.com",
    "dover.com", "recruitcrm.io", "manatal.com",
)
# Job boards whose alerts are worth seeing, though they answer no application.
ALERT_DOMAINS = ("wellfound.com", "torre.ai", "torre.co", "remotive.com", "weworkremotely.com",
                 "jobicy.com", "remoteok.com")
# LinkedIn: a message may be a recruiter (kept for review), a job alert is an
# alert, and the rest (notifications, digests, "updates") is noise.
LINKEDIN_ALERT_RE = re.compile(r"job", re.I)
LINKEDIN_KEEP_RE = re.compile(
    r"sent you a message|new message|inmail|te envi[oó] un mensaje|nuevo mensaje", re.I)
# Sign-up mail from any service: codes, activation, welcome. Never about a job.
ACCOUNT_SUBJECT_RE = re.compile(
    r"verification code|verify your (email|account)|activation code|activate your|confirm your "
    r"(email|account)|welcome to|one[- ]time (code|password)|security alert|new sign[- ]in|"
    r"c[oó]digo de verificaci[oó]n|verifica tu|bienvenid[oa]", re.I)
# A stranger's "we've received..." counts as an acuse only when it's about applying.
APPLICATION_WORD_RE = re.compile(r"appl(y|ied|ying|ication)|postula", re.I)
NOISE_DOMAINS = (
    "accounts.google.com", "google.com", "openai.com", "redditmail.com", "reddit.com",
    "github.com", "discord.com", "facebookmail.com", "instagram.com", "x.com", "twitter.com",
    "spotify.com", "netflix.com", "amazon.com", "steampowered.com", "anthropic.com",
    "medium.com", "substack.com", "youtube.com", "microsoft.com", "apple.com",
)
NOISE_LOCAL_RE = re.compile(r"^(news(letter)?|marketing|promo(tions)?|digest|updates?)\b", re.I)

# Checked in this order: a rejection often thanks you for applying first.
SUGGESTION_RULES = [
    ("rechazo", re.compile(
        r"unfortunately|not (be )?moving forward|move forward with other|other candidates|"
        r"position has (been|now been) filled|decided not to|not to proceed|we regret|"
        r"no longer (considering|available)|lamentablemente|otros candidatos|no avanzar|"
        r"no continuar|no seguir(emos)? adelante|hemos decidido", re.I)),
    ("prueba", re.compile(
        r"assessment|take[- ]home|coding challenge|technical (test|challenge|exercise)|"
        r"hackerrank|codesignal|codility|testgorilla|prueba t[eé]cnica|desaf[ií]o t[eé]cnico|"
        r"test t[eé]cnico|ejercicio t[eé]cnico", re.I)),
    ("entrevista", re.compile(
        r"interview|schedule (a|an|your) (call|chat|meeting)|your availability|calendly\.com|"
        r"book a time|next steps? (is|would be) a (call|chat)|entrevista|agendar|"
        r"tu disponibilidad|una llamada", re.I)),
    ("acuse", re.compile(
        r"received your application|thank(s| you) for (applying|your application|your interest)|"
        r"application (has been )?received|we('ve| have) received|hemos recibido|"
        r"gracias por (postular|aplicar|tu postulaci[oó]n|tu inter[eé]s)", re.I)),
]


# ── Applications log (data/aplicaciones.csv, written by tools/aplicar.py) ──

def leer_registro() -> list[dict]:
    """Every row, with every column (an older log has fewer)."""
    if not REGISTRO.exists():
        return []
    with REGISTRO.open(encoding="utf-8") as f:
        return [{k: r.get(k) or "" for k in CAMPOS_REGISTRO} for r in csv.DictReader(f)]


def guardar_registro(filas: list[dict]):
    # Rewritten whole, so a log with the old columns comes out up to date.
    tmp = REGISTRO.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_REGISTRO)
        w.writeheader()
        w.writerows(filas)
    tmp.replace(REGISTRO)


def sent_applications() -> list[dict]:
    return [f for f in leer_registro() if f["estado"] == "enviada"]


def record_outcome(app_url: str, respuesta: str, nota: str = "") -> bool:
    """Write an outcome onto the application with that URL. False if none has it."""
    if respuesta not in RESPUESTAS:
        raise ValueError(f"respuesta must be one of {RESPUESTAS}")
    filas = leer_registro()
    for f in filas:
        if f["url"] == app_url and f["estado"] == "enviada":
            # Never downgrade: an interview stays an interview if a late "thanks for applying" lands.
            if f["respuesta"] in RESPUESTAS and RESPUESTAS.index(f["respuesta"]) > RESPUESTAS.index(respuesta):
                return True
            f.update(respuesta=respuesta, fecha_respuesta=f"{datetime.now():%Y-%m-%d}",
                     nota=nota[:200] or f["nota"])
            guardar_registro(filas)
            return True
    return False


# ── Matching and classifying ──

def _domain(email: str) -> str:
    return email.rpartition("@")[2].lower()


def _on(domain: str, domains) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúñ]+", " ", (text or "").lower()).strip()


def _company_keys(app: dict) -> set[str]:
    """Ways an email may name the company: its name, and its slug in the job URL."""
    keys = set()
    name = _norm(app["empresa"])
    # "1000 Y Combinator Founders Choose Supabase" is a page title, not a name: skip long ones.
    if name and len(name) >= 3 and len(name.split()) <= 4:
        keys.add(name)
    u = urlparse(app["url"])
    parts = [p for p in u.path.split("/") if p]
    host = u.netloc.lower()
    if parts and any(a in host for a in ("ashbyhq", "greenhouse", "lever", "workable")):
        keys.add(_norm(parts[0]))
    if "himalayas.app" in host and len(parts) >= 2 and parts[0] == "companies":
        keys.add(_norm(parts[1].replace("-", " ")))
    return {k for k in keys if len(k) >= 3}


def match_application(from_email: str, subject: str, body: str, apps: list[dict]) -> str:
    """The URL of the application this email is about, or ''. Latest one wins."""
    domain = _norm(_domain(from_email).replace(".", " "))
    hay = " " + _norm(f"{subject} {body[:3000]}") + " "
    for app in reversed(apps):
        for key in _company_keys(app):
            if f" {key} " in hay or key.replace(" ", "") in domain.replace(" ", ""):
                return app["url"]
    return ""


def suggest(subject: str, body: str) -> str:
    text = f"{subject}\n{body[:4000]}"
    for label, rule in SUGGESTION_RULES:
        if rule.search(text):
            return label
    return ""


def classify(from_email: str, subject: str, body: str, apps: list[dict]) -> dict:
    domain = _domain(from_email)
    local = from_email.partition("@")[0]
    is_ats = _on(domain, ATS_DOMAINS)
    # Account mail (Google saying you shared data with a company you applied
    # to, that company's sign-up code) names the company but answers nothing.
    if not is_ats and (_on(domain, NOISE_DOMAINS) or ACCOUNT_SUBJECT_RE.search(subject)):
        return {"is_ats": 0, "application_url": "", "suggestion": "", "noise": 1}
    if _on(domain, ("linkedin.com",)):
        if LINKEDIN_ALERT_RE.search(local) or LINKEDIN_ALERT_RE.search(subject):
            return {"is_ats": 0, "application_url": "", "suggestion": "alerta", "noise": 0}
        if not LINKEDIN_KEEP_RE.search(subject):
            return {"is_ats": 0, "application_url": "", "suggestion": "", "noise": 1}
    app_url = match_application(from_email, subject, body, apps)
    suggestion = suggest(subject, body)
    if suggestion == "acuse" and not (is_ats or app_url or APPLICATION_WORD_RE.search(subject + body[:500])):
        suggestion = ""
    if not suggestion and _on(domain, ALERT_DOMAINS):
        suggestion = "alerta"
    noise = not (is_ats or app_url or suggestion) and bool(NOISE_LOCAL_RE.match(local))
    return {"is_ats": int(is_ats), "application_url": app_url,
            "suggestion": suggestion, "noise": int(noise)}


def reclassify_all(db_path) -> int:
    """Re-run classify() over stored mail the user hasn't acted on (after a rule change)."""
    import sqlite3
    apps = sent_applications()
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT id, from_email, subject, snippet FROM other_mail "
                       "WHERE status = 'new'").fetchall()
    for mid, frm, subj, snip in rows:
        c = classify(frm, subj, snip, apps)
        con.execute("UPDATE other_mail SET noise=?, is_ats=?, application_url=?, suggestion=? "
                    "WHERE id=?", (c["noise"], c["is_ats"], c["application_url"], c["suggestion"], mid))
    con.commit()
    con.close()
    return len(rows)


def _iso(date_header: str) -> str:
    """UTC, like every other time Harvey stores (the dashboard reads them as UTC)."""
    try:
        when = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        when = None
    if when is None or when.tzinfo is None:
        when = datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Storage ──

async def ensure_table(db):
    await db.executescript(SCHEMA)


async def record_other_mail(state, from_email: str, body: str, meta: dict) -> bool:
    """Keep an inbound email from outside the prospect list. True if it was new."""
    msg_id = meta.get("provider_id") or meta.get("message_id") or ""
    if not msg_id:
        return False
    subject = meta.get("subject", "")
    c = classify(from_email, subject, body, sent_applications())
    async with state._connect() as db:
        await ensure_table(db)
        cur = await db.execute(
            "INSERT OR IGNORE INTO other_mail (id, from_email, subject, snippet, received_at, "
            "thread_ref, noise, is_ats, application_url, suggestion) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (msg_id, from_email.lower(), subject[:300], re.sub(r"\s+", " ", body)[:1500],
             _iso(meta.get("date", "")), meta.get("thread_ref", ""), c["noise"], c["is_ats"],
             c["application_url"], c["suggestion"]))
        await db.commit()
        new = cur.rowcount > 0
    if new and not c["noise"]:
        logger.info(f"Job search: kept mail from {from_email}"
                    + (f" ({c['suggestion']})" if c["suggestion"] else "")
                    + (" — matches an application" if c["application_url"] else ""))
    return new


async def import_recent(state, provider, days: int = 30) -> int:
    """Read the last `days` of the inbox into other_mail (prospects' mail is skipped).

    For mail that arrived before job-search mode kept it: the handler had
    already marked those processed, so it won't read them again."""
    if not hasattr(provider, "_request") or not hasattr(provider, "_parse_message"):
        raise RuntimeError("importing past mail needs the Gmail provider")
    params = {"q": f"in:inbox -from:me newer_than:{int(days)}d", "maxResults": 500}
    kept = 0
    for _page in range(10):
        listing = await provider._request("GET", "/messages", params=dict(params))
        for stub in (listing or {}).get("messages", []):
            data = await provider._request("GET", f"/messages/{stub['id']}", params={"format": "full"})
            msg = provider._parse_message(data) if isinstance(data, dict) else None
            if msg is None or msg.is_bounce or await state.get_prospect_by_email(msg.from_email):
                continue
            kept += await record_other_mail(state, msg.from_email, msg.body, {
                "provider_id": msg.provider_id, "subject": msg.subject,
                "date": msg.date, "thread_ref": msg.thread_ref})
        token = (listing or {}).get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return kept


# ── Prompt block (prompts/job_search_writer.md, prompts/job_search_handler.md) ──

def _projects_text(js) -> str:
    lines, default = [], None
    for p in js.projects:
        if not p.sectors:
            default = default or p
            continue
        lines.append(f"   - {', '.join(p.sectors)}: {p.name}: {p.pitch}")
    if default is not None:
        lines.append(f"   - Any other sector, or no clear sector: {default.name}: {default.pitch}")
    return "\n".join(lines) or "   - (no projects configured: describe the candidate only with the product info above)"


def prompt_block(config, which: str) -> str:
    """The job-search block for the 'writer' or 'handler' prompt, or "" when
    job-search mode is off. Filled in from config, never from the file."""
    js = getattr(config, "job_search", None)
    if js is None or not js.enabled:
        return ""
    from harvey.textio import read_user_text
    try:
        template = read_user_text(PROJECT_ROOT / "prompts" / f"job_search_{which}.md")
    except OSError as e:
        logger.error(f"Job search: couldn't read prompts/job_search_{which}.md: {e}")
        return ""
    persona = config.persona
    signature = (getattr(persona, "signature", "") or persona.name).strip()
    values = {
        "candidate_name": persona.name,
        "candidate_intro": js.intro or persona.role,
        "role_sought": js.role_sought,
        "projects": _projects_text(js),
        "project_notes": f"   {js.project_notes.strip()}" if js.project_notes.strip() else "",
        "signature": signature,
        "signature_es_rule": (f"- In Spanish emails use this signature instead:\n{js.signature_es.strip()}"
                              if js.signature_es.strip() else ""),
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template.strip() + "\n\n"


# ── Paths for the tools in tools/ (aplicar.py, vacantes_remotas.py) ──

def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def tool_settings() -> dict:
    """profile (aplicar.py's answers), vacancies dir, country and intro, from
    `job_search` in the config; defaults inside the repo when it's missing."""
    try:
        from harvey.config import load_config
        js = load_config().job_search
    except Exception:
        from harvey.config import JobSearchConfig
        js = JobSearchConfig()
    return {"profile": _resolve(js.profile_path), "vacancies_dir": _resolve(js.vacancies_dir),
            "country": js.country.strip(), "intro": js.intro.strip(),
            "regions": [r.strip().lower() for r in js.regions if r.strip()]}
