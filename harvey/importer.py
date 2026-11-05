"""Bulk-load companies and prospects from a CSV you already have.

Runs as a dry run unless told to apply. Rows land through
``StateManager.add_company`` / ``add_prospect``, so identity stays Harvey's own:
a company is its domain (or an ``external_id`` when it has no website), a
prospect is its email. Importing the same file twice changes nothing.

An imported address is only as trustworthy as where it came from, so it lands
as ``guess`` (never sent) unless the file says otherwise or ``verify`` is on.
"""

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "aol.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "comcast.net",
}
VALID_STATUSES = {"verified", "risky", "guess", "invalid"}
EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")

# field -> normalised header names that map to it; the first one found wins
ALIASES = {
    "name": ("firmname", "companyname", "company", "name", "firm"),
    "website": ("website", "websiteurl", "url", "site"),
    "domain": ("domain",),
    "email": ("email", "emailaddress"),
    "first_name": ("firstname", "first"),
    "last_name": ("lastname", "last"),
    "full_name": ("attorneyname", "contactname", "fullname"),
    "title": ("title", "jobtitle"),
    "phone": ("phone", "phonenumber"),
    "location": ("city", "location"),
    "linkedin_url": ("linkedinurl", "linkedinprofileurl", "linkedin"),
    "email_status": ("emailstatus",),
    "notes": ("fitnote", "personalization"),
}


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def _domain_of(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    host = urlparse(value if "//" in value else "//" + value).hostname or ""
    return host.removeprefix("www.")


@dataclass
class ImportReport:
    rows: int = 0
    companies_new: int = 0
    prospects_new: int = 0
    rejected: Counter = field(default_factory=Counter)
    rejects: list = field(default_factory=list)  # (line, reason, label) for --rejects

    def reject(self, line: int, reason: str, label: str) -> None:
        self.rejected[reason] += 1
        self.rejects.append((line, reason, label))


def _column_map(headers: list[str]) -> dict[str, str]:
    by_key: dict[str, str] = {}
    for h in headers:
        by_key.setdefault(_key(h), h)
    return {f: by_key[a] for f, names in ALIASES.items()
            for a in names if a in by_key}


async def import_csv(
    state: StateManager,
    path: str | Path,
    *,
    apply: bool = False,
    source: str = "import",
    industry: str = "",
    verify: bool = False,
    env=None,
) -> ImportReport:
    """Import ``path``. Without ``apply`` nothing is written."""
    report = ImportReport()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _column_map(reader.fieldnames or [])
        if "email" not in cols or not ({"name", "website", "domain"} & cols.keys()):
            raise ValueError(
                "CSV needs an email column and a company name, website or domain column "
                f"(found: {', '.join(reader.fieldnames or []) or 'none'})")

        def get(row, name):
            return (row.get(cols[name]) or "").strip() if name in cols else ""

        seen: set[str] = set()
        companies_seen: set[str] = set()
        for line, row in enumerate(reader, start=2):
            report.rows += 1
            name = get(row, "name")
            email = get(row, "email").lower()
            label = name or email
            if "@" not in email:
                report.reject(line, "no email (blank or placeholder)", label)
                continue
            if not EMAIL_RE.match(email):
                report.reject(line, "malformed email", label)
                continue
            if email in seen:
                report.reject(line, "duplicate email in file", label)
                continue
            seen.add(email)
            domain = _domain_of(get(row, "domain") or get(row, "website"))
            mail_domain = email.rsplit("@", 1)[1]
            if not domain and mail_domain not in FREE_MAIL:
                domain = mail_domain
            if not name and not domain:
                report.reject(line, "no company name or domain", label)
                continue
            if await state.get_prospect_by_email(email):
                report.reject(line, "already in database", label)
                continue

            status = get(row, "email_status").lower()
            if status not in VALID_STATUSES:
                status = "guess"
            if verify and status == "guess":
                from harvey.integrations.email_finder import verify_published_address
                status = await verify_published_address(email, env)
            if status == "invalid":
                report.reject(line, "email failed verification", label)
                continue

            location = get(row, "location")
            external_id = "" if domain else f"{source}:{_key(name)}:{_key(location)}"
            report.prospects_new += 1
            if (domain or external_id) not in companies_seen:
                companies_seen.add(domain or external_id)
                known = (await state.get_company_by_domain(domain) if domain
                         else await state.get_company_by_external_id(external_id))
                report.companies_new += known is None
            if not apply:
                continue
            company_id = await state.add_company(Company(
                name=name, domain=domain, website=get(row, "website"),
                industry=industry, location=location, phone=get(row, "phone"),
                source=source, external_id=external_id,
            ))
            first, last = get(row, "first_name"), get(row, "last_name")
            if not (first or last):
                first, _, last = get(row, "full_name").partition(" ")
            await state.add_prospect(Prospect(
                company_id=company_id, first_name=first, last_name=last.strip(),
                email=email, email_status=status,
                email_verified=status == "verified",
                phone=get(row, "phone"), linkedin_url=get(row, "linkedin_url"),
                title=get(row, "title"), source=source, company=name,
                industry=industry, personalization_notes=get(row, "notes"),
            ))
    return report
