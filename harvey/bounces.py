"""Bounces: which ones are Harvey's, why they happened, and what to tighten.

A sending mailbox is often someone's everyday mailbox, so not every bounce
in it is about mail Harvey sent. A bounce counts toward the kill switch only
when it's for an address Harvey emailed: matched by the bounce's In-Reply-To,
or by the failed recipient the notice names. Everything else is the owner's
own mail and is logged, not counted.

For Harvey's own bounces this reads the reason the receiving server gave and
turns it, with how the address was found, into suggestions for which rule to
tighten. The suggestions are advice for the operator; nothing changes on its
own.
"""

from __future__ import annotations

import re

_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Lines a bounce uses to say why: an SMTP reply ("550 5.1.1 ..."), a
# status code ("Status: 5.1.1"), or the plain-language sentence providers add.
_REASON_LINES = (
    re.compile(r"\b[45]\d\d[ -][45]\.\d{1,3}\.\d{1,3}\b.*", re.I),
    re.compile(r"\b[45]\.\d{1,3}\.\d{1,3}\b.*", re.I),
    re.compile(r"(?:diagnostic-code|status):.*", re.I),
    re.compile(r".*(?:does not exist|doesn't exist|not found|unknown user|user unknown|"
               r"no such user|mailbox unavailable|address rejected|couldn't be found|"
               r"mailbox (?:is )?full|over quota|quota exceeded|blocked|spam|"
               r"rejected|denied|not permitted).*", re.I),
)

KINDS = {
    "no_mailbox": "the mailbox doesn't exist",
    "mailbox_full": "the mailbox is full",
    "blocked": "the receiving server refused your mail",
    "other": "the receiving server returned it",
}


def failed_addresses(subject: str, body: str, headers: dict | None = None) -> list[str]:
    """Every address a bounce notice names, lower-cased, in order, deduped.
    Headers that say it outright (X-Failed-Recipients) come first."""
    found = []
    for key, value in (headers or {}).items():
        if str(key).lower() in ("x-failed-recipients", "original-recipient", "final-recipient"):
            found += _ADDRESS.findall(str(value))
    found += _ADDRESS.findall(f"{subject}\n{body}")
    return list(dict.fromkeys(a.lower().strip(".") for a in found))


def bounce_reason(body: str) -> str:
    """The receiving server's explanation, one line, or ""."""
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip()]
    for pattern in _REASON_LINES:
        for line in lines:
            m = pattern.search(line)
            if m:
                return re.sub(r"\s+", " ", m.group(0)).strip()[:200]
    return ""


def bounce_kind(reason: str) -> str:
    """no_mailbox, mailbox_full, blocked or other."""
    r = (reason or "").lower()
    if re.search(r"5\.2\.2|mailbox (is )?full|over quota|quota exceeded|insufficient storage", r):
        return "mailbox_full"
    if re.search(r"5\.7\.\d|blocked|spam|blacklist|blocklist|reputation|policy|not permitted|denied", r):
        return "blocked"
    if re.search(r"5\.1\.[0-3]|5\.1\.10|does not exist|doesn't exist|not found|unknown user|"
                 r"user unknown|no such user|mailbox unavailable|address rejected|"
                 r"couldn't be found|recipient (address )?rejected|invalid recipient", r):
        return "no_mailbox"
    return "other"


def how_found(source: str) -> str:
    """How Harvey came by the address, in plain words."""
    source = source or ""
    if source.startswith("finder:"):
        return f"email finder ({source.split(':', 1)[1].title()})"
    return {
        "company_inbox": "general inbox published on their site",
        "scraped_mailto": "address published on their site",
        "hunter_domain_search": "Hunter domain search",
        "company_website": "built from the company's address format",
        "known_company_backfill": "built from the company's address format",
    }.get(source, source.replace("_", " ") or "unknown")


def suggestions(kind: str, *, source: str = "", email_status: str = "",
                catch_all: bool = False, mx_type: str = "",
                send_to_risky: bool = False) -> list[str]:
    """What to tighten, from the bounce's cause and how the address was found."""
    tips = []
    if kind == "blocked":
        tips.append("This is about your sender reputation, not the address: check SPF, DKIM "
                    "and DMARC with `harvey warmup`, lower channels.email.max_daily_sends, "
                    "and keep the mailbox on a warmup service.")
    elif kind == "mailbox_full":
        tips.append("The mailbox exists but is full: a temporary problem. Harvey marked it "
                    "invalid anyway; leave it, or try them again later by another route.")
    if kind in ("no_mailbox", "other"):
        if email_status == "risky" or catch_all:
            tips.append("The domain accepts every address (catch-all), so no verifier could "
                        "confirm this one. " + ("Turn channels.email.send_to_risky off."
                                                if send_to_risky else
                                                "Keep channels.email.send_to_risky off."))
        if source.startswith("finder:"):
            tips.append("The address came from an email finder. Add a second verifier "
                        "(ZEROBOUNCE_API_KEY) so finder addresses are checked by a verifier, "
                        "not only by the finder, or ask that finder later in EMAIL_FINDERS.")
        elif source in ("company_website", "known_company_backfill"):
            tips.append("The address was built from the company's format. The person may "
                        "use another alias or have left: an email finder (EMAIL_FINDERS) can "
                        "look up their own address.")
        elif source == "company_inbox":
            tips.append("General inboxes (info@, sales@) are often retired without notice. "
                        "Prefer a named contact at this company.")
        if email_status == "verified" and mx_type in ("microsoft", "google"):
            tips.append("It verified but still bounced: verifiers can't always see into "
                        "Microsoft 365 and Google Workspace. ZeroBounce is the most reliable "
                        "for them (ZEROBOUNCE_API_KEY).")
        if email_status == "verified":
            tips.append("Verification can go stale: the longer an address waits between "
                        "verifying and sending, the likelier it bounces.")
    return tips


# ── The mailbox monitor: bounces of mail Harvey didn't send ──
# They aren't Harvey's to fix, but they're still a reputation signal for the
# mailbox: the owner's own mail is bouncing, or another tool is sending cold
# email from it. Enough of them in a window pauses Harvey's sending too, with
# a reason that says so, so the owner can check or move Harvey elsewhere.

MAILBOX_SINCE_KEY = "mailbox_bounces_since"


def mailbox_window_start(days: int, now=None) -> str:
    """Where the mailbox monitor's window starts: ``days`` ago, in the format
    actions.created_at uses. A resume also restarts the count (see
    StateManager.resume_sending), counted by log position."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def resumed_after_row(value) -> int:
    """The log position the last resume recorded (0 if never, or unreadable)."""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def mailbox_pause_reason(count: int, days: int, limit: int) -> str:
    return (f"{count} bounces of mail Harvey didn't send in the last {days} days "
            f"(limit {limit}). This mailbox's reputation may be at risk: another tool may be "
            "sending from it, or its own mail is bouncing. Check it, or move Harvey to "
            "another mailbox, before resuming")
