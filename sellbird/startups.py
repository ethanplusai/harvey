"""What Harvey knows about a prospect's company as a startup.

Three profile signals mark one, each read off the company's own site:
YC_BACKED, ACCELERATOR, and a FUNDING_ROUND dated within the last 18
months. The writer and the reply handler use the founder playbook
(skills/selling_to_startups.md) for those, and these facts go into the
writer's prompt as they are. An old or undated raise alone doesn't make a
startup (a 2013 Series A, or a law firm's misread "$100M"), and "just
raised" is only an angle while it's true.
"""

import re
from datetime import date

# Past this, a raise is history, not news (about 18 months).
FUNDING_RECENT_DAYS = 548
_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")


def funding_text(found: dict) -> str:
    """The FUNDING_ROUND value as stored: "Series A · $12M · 2025-03"."""
    return " · ".join(p for p in (found.get("round"), found.get("amount"), found.get("date")) if p)


def parse_funding(text: str) -> dict:
    """funding_text, read back into its parts (any may be "")."""
    out = {"round": "", "amount": "", "date": ""}
    for part in (p.strip() for p in (text or "").split("·")):
        if not part:
            continue
        if _DATE_RE.match(part):
            out["date"] = part
        elif part[0] in "$€£":
            out["amount"] = part
        else:
            out["round"] = part
    return out


def _as_date(when: str) -> date | None:
    """The earliest day a stated date could mean: "2025" is 1 January, so an
    imprecise date never makes a raise look newer than it is."""
    m = _DATE_RE.match(when or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
    except ValueError:
        return None


def funding_recency(when: str, today: date | None = None) -> str:
    """"recent", "old" (over ~18 months) or "unknown" (no date found)."""
    day = _as_date(when)
    if day is None:
        return "unknown"
    return "recent" if ((today or date.today()) - day).days <= FUNDING_RECENT_DAYS else "old"


def _funding_fact(found: dict, today: date | None) -> str:
    what = ", ".join(p for p in (found["round"], found["amount"]) if p) or "a funding round"
    when = found["date"]
    recency = funding_recency(when, today)
    if recency == "recent":
        months = max(0, ((today or date.today()) - _as_date(when)).days // 30)
        return (f"- Raised: {what}, announced {when} (about {months} month(s) ago), "
                "as their own site says")
    if recency == "old":
        return (f"- Raised: {what}, announced {when}, as their own site says. That is over 18 "
                "months ago, so not recent: don't write as if they just raised")
    return (f"- Raised: {what}, as their own site says, but no date was found: "
            "don't call it recent")


async def startup_facts(state, company_id: str, today: date | None = None) -> list[str] | None:
    """Fact lines for the writer's prompt when the company is a startup
    (YC, another accelerator, or a raise it announced in the last 18
    months), else None. A failed read is just "not known to be one", never
    a reason not to write."""
    if not company_id or state is None:
        return None
    facts: list[str] = []

    async def read(name):
        lookup = getattr(state, name, None)
        if lookup is None:
            return None
        try:
            return await lookup(company_id)
        except Exception:
            return None

    yc = await read("yc_batch")
    if yc is not None:
        facts.append("- Backed by Y Combinator" + (f" (YC {yc})" if yc else "") + ", as their own site says")
    program = await read("accelerator")
    if program is not None:
        facts.append(f"- Went through an accelerator: {program}, as their own site says" if program
                     else "- Went through a startup accelerator (a search matched it on their own site)")
    funding = await read("funding_round")
    recent = funding is not None and funding_recency(funding["date"], today) == "recent"
    if not (facts or recent):
        return None
    if funding is not None:
        facts.append(_funding_fact(funding, today))
    return facts
