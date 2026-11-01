"""LinkedIn message drafts after applying (Morthy, job-search fork).

Like tools/aplicar.py, Morthy prepares and you send: it finds the person who
decides at a company you applied to (the same search as the post-application
note, but a name is enough: no email needed), writes a connection note that
fits LinkedIn's 300-character limit and a slightly longer message for once
you're connected, and gives you the profile link (or a people search when
there's no name). Nothing here logs in to LinkedIn or touches your account:
you open the link and paste the text yourself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from urllib.parse import quote

from harvey import job_search
from harvey.follow_up import SKIP_OUTCOMES, PostApplyFollowUp, _applied_at, title_names_other_org
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.linkedin_drafts")

NOTE_LIMIT = 300            # LinkedIn's limit on a connection note
MESSAGE_LIMIT = 1200         # ours: well under LinkedIn's, and room for a CV link
MAX_DAYS_AFTER_APPLYING = 21

SCHEMA = """
CREATE TABLE IF NOT EXISTS linkedin_drafts (
    app_url TEXT PRIMARY KEY,
    company TEXT DEFAULT '',
    job_title TEXT DEFAULT '',
    person TEXT DEFAULT '',          -- '' = nobody named: the link is a people search
    person_title TEXT DEFAULT '',
    link TEXT DEFAULT '',
    note TEXT DEFAULT '',
    message TEXT DEFAULT '',
    status TEXT DEFAULT 'new',       -- new | noted (note sent) | done (message sent) | skipped
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


# A team page's "name" that is really a label ("Venture Startup", "Our Team").
NOT_A_NAME = {"startup", "venture", "team", "our", "the", "company", "group", "labs", "inc",
              "llc", "ltd", "solutions", "technologies", "technology", "studio", "agency",
              "partners", "consulting", "careers", "jobs", "contact", "about", "founder",
              "ceo", "cto", "admin", "support", "hello", "info", "marketing", "sales"}


def looks_like_person(first: str, last: str) -> bool:
    words = f"{first} {last}".lower().split()
    return len(words) >= 2 and not any(w.strip(".,") in NOT_A_NAME for w in words)


def _fit(text: str, limit: int) -> str:
    """At most `limit` characters, cut after the last whole sentence that fits."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind(".\n"))
    return cut[:end + 1] if end > 0 else cut.rsplit(" ", 1)[0]


def people_search_url(keywords: str) -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(keywords)


class LinkedInDrafter:
    def __init__(self, brain, state, config, env):
        self.config = config
        self.state = state
        self.brain = brain
        self.finder = PostApplyFollowUp(brain, state, config, env)

    async def _db(self, sql: str, params: tuple = (), fetch: bool = False):
        async with self.state._connect() as db:
            await db.executescript(SCHEMA)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall() if fetch else None
            await db.commit()
            return rows

    async def drafts(self, statuses: tuple = ("new", "noted")) -> list[dict]:
        cols = ["app_url", "company", "job_title", "person", "person_title", "link",
                "note", "message", "status", "created_at"]
        where = f"WHERE status IN ({', '.join('?' for _ in statuses)})" if statuses else ""
        params = tuple(statuses)
        rows = await self._db(f"SELECT {', '.join(cols)} FROM linkedin_drafts {where} "
                              "ORDER BY created_at", params, fetch=True)
        return [dict(zip(cols, r)) for r in rows]

    async def set_status(self, app_url: str, status: str):
        await self._db("UPDATE linkedin_drafts SET status = ? WHERE app_url = ?", (status, app_url))

    async def pending(self) -> list[dict]:
        """One application per company (the latest), with every role applied
        for there in "roles": one message to a person, not one per opening."""
        rows = await self._db("SELECT app_url, lower(company) FROM linkedin_drafts", fetch=True)
        done_urls = {r[0] for r in rows}
        done_companies = {r[1] for r in rows}
        now, by_company = datetime.now(), {}
        for app in job_search.sent_applications():
            when = _applied_at(app)
            company = app["empresa"].strip().lower()
            if (app["url"] in done_urls or company in done_companies or when is None
                    or not company or app["respuesta"] in SKIP_OUTCOMES
                    or now - when > timedelta(days=MAX_DAYS_AFTER_APPLYING)):
                continue
            entry = by_company.setdefault(company, {**app, "roles": []})
            entry.update({k: app[k] for k in ("url", "fecha", "titulo")})   # latest wins
            entry["roles"].append(app["titulo"])
        return list(by_company.values())

    async def _person(self, app: dict):
        """(name, title, link) of who to write to; name '' means a people search link."""
        company = app["empresa"].strip()
        self.finder.scout._queries_this_cycle = 0   # a fresh search allowance per company
        # The post-application note may have found someone already.
        rows = await self._db(
            "SELECT p.first_name, p.last_name, p.title, p.source_url FROM post_apply a "
            "JOIN prospects p ON p.id = a.prospect_id WHERE a.app_url = ?", (app["url"],), fetch=True) \
            if await self._has_post_apply() else []
        if rows and looks_like_person(rows[0][0], rows[0][1]):
            first, last, title, url = rows[0]
        else:
            domain = await self.finder.find_domain(company, app["url"])
            first = last = title = url = ""
            # "1000 Y Combinator Founders Choose Supabase" is a page title: search by "supabase".
            if domain and len(company.split()) > 3:
                company = domain.split(".")[0]
            record = await self.state.get_company_by_domain(domain) if domain else None
            # A big company's homepage names customers and investors, not its team.
            big = bool(record) and await self.finder._is_big(record.id)
            if domain and not big:
                members = await self.finder.scout._scrape_team_page(domain)
                ranked = sorted(members, key=lambda m: -(m.get("rank") or 0))
                for m in ranked:
                    if looks_like_person(m.get("first_name", ""), m.get("last_name", "")) \
                            and not title_names_other_org(m.get("title", ""), company) and (
                            self.finder.scout._title_matches_icp(m.get("title", "")) or m.get("owner")):
                        first, last = m["first_name"], m["last_name"]
                        title, url = m.get("title", "") or "Owner", ""
                        break
            if not first:
                found = await self.finder._person_via_search(company, domain, "", need_email=False)
                if found and looks_like_person(found.first_name, found.last_name):
                    first, last, title, url = found.first_name, found.last_name, found.title, found.source_url
        name = f"{first} {last}".strip()
        if name:
            link = url if "linkedin.com/in/" in (url or "") else people_search_url(f"{name} {company}")
        else:
            titles = list(self.config.icp.titles)[:1] or ["CTO"]
            link = people_search_url(f"{company} {titles[0]}")
        return name, title, link

    async def _has_post_apply(self) -> bool:
        rows = await self._db("SELECT name FROM sqlite_master WHERE type='table' AND name='post_apply'",
                              fetch=True)
        return bool(rows)

    async def _write(self, app: dict, name: str, title: str) -> dict | None:
        p, persona, js = self.config.product, self.config.persona, self.config.job_search
        days = max(0, (datetime.now() - _applied_at(app)).days)
        when = "today" if days == 0 else ("yesterday" if days == 1 else f"{days} days ago")
        greeting = f"Hi {name.split()[0]}," if name else "Hi,"
        roles = app.get("roles") or [app["titulo"]]
        applied = (f'the "{roles[0]}" role' if len(roles) == 1
                   else "these roles: " + ", ".join(f'"{r}"' for r in roles))
        prompt = f"""You write LinkedIn messages for {persona.name}, {js.intro or persona.role}, who is
looking for {js.role_sought}. {persona.name} applied for {applied} at {app['empresa']}
through the company's own careers page {when}, and now wants to reach
{name or 'someone on the engineering team'}{f' ({title})' if title else ''} on LinkedIn.

Tone: {persona.tone}

Facts about {persona.name}. Use ONLY these, never invent experience, numbers or availability:
{fence(p.description, 'candidate profile')}
Projects (pick the one that best fits {app['empresa']}, if any):
{job_search._projects_text(js)}

{UNTRUSTED_RULE}

Write two texts, in the language the tone above says for this company:
1. "note": a connection request note. At most 280 characters, counting spaces. Start with
   "{greeting}". Say {persona.name} applied (name the role, or say "two roles" etc. if several)
   and would be glad to connect.
   No links, no emoji, no hashtags.
2. "message": for once they accept. 60 to 110 words. Start with "{greeting}". Thank them for
   connecting, mention the application in one line, give ONE project with its real facts,
   and close softly (grateful if the application could be considered, or to be pointed to
   the right person). No links except, at most, the candidate's CV link if it's in the facts.
   Never ask them to reply, never suggest times. End with "Thank you for your time."

Return ONLY JSON: {{"note": "...", "message": "..."}}"""
        result = await self.brain.think_json(prompt, session_id="harvey-linkedin",
                                             agent="writer", task="linkedin_draft")
        if not isinstance(result, dict):
            return None
        note = str(result.get("note") or "").strip()
        message = str(result.get("message") or "").strip()
        if not note or not message:
            return None
        return {"note": _fit(note, NOTE_LIMIT), "message": _fit(message, MESSAGE_LIMIT)}

    async def prepare(self, limit: int = 5) -> int:
        made = 0
        for app in (await self.pending())[:limit]:
            try:
                name, title, link = await self._person(app)
                draft = await self._write(app, name, title)
            except Exception as e:
                logger.warning(f"LinkedIn draft for {app['empresa']} failed: {e}")
                continue
            if draft is None:
                logger.warning(f"LinkedIn draft for {app['empresa']}: no answer from the model.")
                continue
            await self._db(
                "INSERT OR REPLACE INTO linkedin_drafts (app_url, company, job_title, person, "
                "person_title, link, note, message) VALUES (?,?,?,?,?,?,?,?)",
                (app["url"], app["empresa"], " / ".join(app.get("roles") or [app["titulo"]]), name, title, link,
                 draft["note"], draft["message"]))
            made += 1
        return made
