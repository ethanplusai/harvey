"""Deliverability readiness - is this sending domain safe to cold email from?

Everything here is free and read-only: public DNS, RDAP, DNS blocklists, and
Harvey's own send history. None of it can measure inbox reputation directly
(only mailbox providers know that), so the report also points at the two
things that can: Google Postmaster Tools and an inbox-placement test.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

import dns.exception
import dns.resolver
import httpx

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

# Selectors used by the common mailbox providers, tried in order when the
# operator hasn't named one (warmup.dkim_selector).
COMMON_DKIM_SELECTORS = (
    "google", "default", "selector1", "selector2", "k1", "k2", "s1", "s2",
    "mail", "dkim", "fm1", "fm2", "fm3", "zoho", "zmail", "mxvault",
    "protonmail", "protonmail2", "mandrill", "smtp", "sig1", "everlytickey1",
)

# (zone, how a "query refused" answer looks) - public resolvers like 8.8.8.8
# are blocked by these lists and get a sentinel answer instead of a verdict.
DOMAIN_BLOCKLISTS = (
    ("dbl.spamhaus.org", lambda ip: ip.startswith("127.255.255.")),
    ("multi.surbl.org", lambda ip: ip == "127.0.0.1"),
    ("multi.uribl.com", lambda ip: ip == "127.0.0.1"),
)
IP_BLOCKLISTS = (
    ("zen.spamhaus.org", lambda ip: ip.startswith("127.255.255.")),
    ("bl.spamcop.net", lambda ip: False),
)

# Where to look a domain or IP up by hand when a list refuses public DNS.
MANUAL_LOOKUP = {
    "dbl.spamhaus.org": "https://check.spamhaus.org",
    "zen.spamhaus.org": "https://check.spamhaus.org",
    "multi.surbl.org": "https://www.surbl.org/surbl-analysis",
    "multi.uribl.com": "https://admin.uribl.com/",
    "bl.spamcop.net": "https://www.spamcop.net/bl.shtml",
}

MIN_DOMAIN_AGE_DAYS = 30
GOOD_DOMAIN_AGE_DAYS = 90
MAX_BOUNCE_RATE = 0.03
MIN_SENDS_FOR_RATES = 20


@dataclass
class Check:
    name: str
    status: str
    detail: str
    unknown: bool = False  # the lookup itself failed: no verdict either way


DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


class LookupFailed(Exception):
    """DNS couldn't answer (timeout, no reachable server): not the same as
    'no such record', so a check reports it as unknown, never as missing."""


def _resolve(name: str, rtype: str):
    try:
        return list(dns.resolver.resolve(name, rtype, lifetime=8))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as e:
        raise LookupFailed(type(e).__name__) from e


def _txt(name: str) -> list[str]:
    return [b"".join(r.strings).decode("utf-8", "replace") for r in _resolve(name, "TXT")]


def _a(name: str) -> list[str]:
    return [r.address for r in _resolve(name, "A")]


def _unknown(name: str, e: LookupFailed) -> Check:
    return Check(name, INFO, f"Couldn't look this up ({e}). Check your connection and run again.",
                 unknown=True)


def check_mx(domain: str) -> Check:
    try:
        mx = sorted(_resolve(domain, "MX"), key=lambda r: r.preference)
    except LookupFailed as e:
        return _unknown("MX", e)
    if not mx:
        return Check("MX", FAIL, "No MX record - replies and bounces have nowhere to go.")
    return Check("MX", OK, ", ".join(str(r.exchange).rstrip(".") for r in mx[:3]))


def check_spf(domain: str) -> Check:
    try:
        records = [t for t in _txt(domain) if t.lower().startswith("v=spf1")]
    except LookupFailed as e:
        return _unknown("SPF", e)
    if not records:
        return Check("SPF", FAIL, "No SPF record. Add the TXT record your mail provider gives you.")
    if len(records) > 1:
        return Check("SPF", FAIL, f"{len(records)} SPF records - receivers treat that as no SPF. Merge them.")
    spf = records[0]
    terms = spf.lower().split()
    if "+all" in terms or "all" in terms:
        return Check("SPF", FAIL, f"'+all' lets anyone send as you: {spf}")
    lookups = sum(t.lstrip("+-~?").split(":")[0].split("=")[0].split("/")[0] in
                  ("include", "a", "mx", "ptr", "exists", "redirect") for t in terms)
    if lookups > 10:
        # Top-level terms only; includes add their own lookups on top.
        return Check("SPF", FAIL, f"{lookups} DNS lookups (limit is 10) - SPF will permerror.")
    if "?all" in terms:
        return Check("SPF", WARN, f"'?all' is neutral; use '~all' or '-all': {spf}")
    if not any(t.lstrip("+-~?") == "all" or t.startswith("redirect=") for t in terms):
        return Check("SPF", WARN, f"No 'all' at the end, so it ends neutral; add '~all': {spf}")
    return Check("SPF", OK, spf)


def check_dmarc(domain: str) -> Check:
    try:
        records = [t for t in _txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
    except LookupFailed as e:
        return _unknown("DMARC", e)
    if not records:
        return Check("DMARC", FAIL, "No DMARC record. Gmail and Yahoo require one for bulk senders. "
                     f"Start with TXT _dmarc.{domain} = \"v=DMARC1; p=none; rua=mailto:you@{domain}\"")
    if len(records) > 1:
        return Check("DMARC", FAIL, f"{len(records)} DMARC records - receivers ignore them all. Keep one.")
    tags = {k.strip().lower(): v for k, v in (
        part.strip().split("=", 1) for part in records[0].split(";") if "=" in part)}
    policy = tags.get("p", "").strip().lower()
    if not policy:
        return Check("DMARC", WARN, f"No p= policy tag - add p=none to start: {records[0]}")
    if policy == "none":
        return Check("DMARC", WARN, f"p=none (monitoring only) - fine while warming; "
                     f"move to quarantine once reports look clean. {records[0]}")
    return Check("DMARC", OK, records[0])


def check_dkim(domain: str, selector: str = "") -> Check:
    selectors = (selector,) if selector else COMMON_DKIM_SELECTORS
    for sel in selectors:
        try:
            records = _txt(f"{sel}._domainkey.{domain}")
        except LookupFailed as e:
            return _unknown("DKIM", e)
        for record in records:
            if "p=" in record.replace(" ", "") and "p=;" not in record.replace(" ", "") + ";":
                return Check("DKIM", OK, f"selector '{sel}' found")
    if selector:
        return Check("DKIM", FAIL, f"No DKIM key at {selector}._domainkey.{domain}.")
    return Check("DKIM", WARN, "No key at any common selector. If your provider uses a custom one, "
                 "set channels.email.warmup.dkim_selector; otherwise turn DKIM on in your provider.")


def check_domain_age(domain: str) -> Check:
    try:
        r = httpx.get(f"https://rdap.org/domain/{domain}", timeout=15, follow_redirects=True)
        r.raise_for_status()
        events = r.json().get("events", [])
    except Exception as e:
        return Check("Domain age", INFO, f"Couldn't look up registration date ({type(e).__name__}).")
    registered = next((e.get("eventDate") for e in events if e.get("eventAction") == "registration"), None)
    if not registered:
        return Check("Domain age", INFO, "Registry didn't publish a registration date.")
    try:
        age = (date.today() - datetime.fromisoformat(registered.replace("Z", "+00:00")).date()).days
    except ValueError:
        return Check("Domain age", INFO, f"Registry gave an unreadable date ({registered[:40]}).")
    status = FAIL if age < MIN_DOMAIN_AGE_DAYS else WARN if age < GOOD_DOMAIN_AGE_DAYS else OK
    note = {FAIL: " - brand-new domains are distrusted; warm only, no cold email yet.",
            WARN: " - still young; keep volume low.", OK: ""}[status]
    return Check("Domain age", status, f"{age} days (registered {registered[:10]}){note}")


def _blocklist_checks(query: str, label: str, lists) -> list[Check]:
    checks = []
    for zone, refused in lists:
        try:
            hits = _a(f"{query}.{zone}")
        except LookupFailed as e:
            checks.append(_unknown(f"{label} {zone}", e))
            continue
        if not hits:
            checks.append(Check(f"{label} {zone}", OK, "not listed"))
        elif any(refused(ip) for ip in hits):
            checks.append(Check(f"{label} {zone}", INFO,
                                "list refused the query (common via public DNS like 8.8.8.8) - "
                                f"check manually at {MANUAL_LOOKUP.get(zone, zone)}"))
        else:
            checks.append(Check(f"{label} {zone}", FAIL, f"LISTED ({', '.join(hits)})"))
    return checks


def check_domain_blocklists(domain: str) -> list[Check]:
    return _blocklist_checks(domain, "Blocklist", DOMAIN_BLOCKLISTS)


def check_ip_blocklists(smtp_host: str) -> list[Check]:
    try:
        ips = _a(smtp_host)
    except LookupFailed as e:
        return [_unknown("Sending IP", e)]
    if not ips:
        return [Check("Sending IP", INFO, f"Couldn't resolve {smtp_host}.")]
    reversed_ip = ".".join(reversed(ips[0].split(".")))
    return _blocklist_checks(reversed_ip, f"IP {ips[0]}", IP_BLOCKLISTS)


def check_warmup(warmup, ceiling: int, today: date | None = None,
                 provider: str = "") -> Check:
    if provider == "instantly":
        return Check("Warmup", INFO, "Harvey's ramp doesn't apply to Instantly: use Instantly's "
                     "own warmup and per-account daily limit.")
    days = warmup.days_in(today)
    if days is None:
        return Check("Warmup", WARN, "No warmup start date set, so sends aren't ramped. Once your warmup "
                     "service is on, set channels.email.warmup.start_date: YYYY-MM-DD.")
    if days < 0:
        return Check("Warmup", WARN, f"Starts {warmup.start_date} (in {-days} days).")
    if days < warmup.min_days:
        return Check("Warmup", FAIL, f"Day {days} of {warmup.min_days} - warming only, no cold email "
                     f"until {warmup.min_days - days} more day(s) pass.")
    cap = warmup.daily_cap(ceiling, today)
    if cap <= 0 and warmup.daily_increase <= 0:
        return Check("Warmup", WARN, "Today's cold cap is 0 and won't grow: raise start_sends "
                     "or daily_increase.")
    if cap >= ceiling:
        ramp = "full volume"
    elif warmup.daily_increase <= 0:
        ramp = f"stays at {cap}/day (daily_increase is 0)"
    else:
        full_on = warmup.min_days + -(-(ceiling - warmup.start_sends) // warmup.daily_increase)
        ramp = f"reaches {ceiling}/day on day {full_on}"
    return Check("Warmup", OK, f"Day {days}: today's cap is {cap}/day ({ramp}). "
                 "Keep the warmup service running alongside Harvey.")


# Warmup spam at or above this, over enough mail to judge, means the mailbox
# isn't ready whatever the day count says.
MAX_WARMUP_SPAM = 0.05
MIN_WARMUP_SAMPLE = 20


def check_warmup_service(health, warmup=None, *, error: str = "", other_service: bool = False,
                         today: date | None = None) -> Check:
    """Is the sending mailbox actually warmed? Read from TrulyInbox when it's connected."""
    from harvey.integrations.trulyinbox import AFFILIATE_TAG, AFFILIATE_URL
    if error:
        return Check("Warmup service", WARN, f"Couldn't read TrulyInbox: {error}")
    if health is None:
        if other_service:
            return Check("Warmup service", INFO, "Using another warmup service: check its "
                         "dashboard for the inbox rate before cold email.")
        return Check("Warmup service", INFO, "Not connected, so Harvey can't see whether this "
                     "mailbox is warmed. We recommend TrulyInbox: it warms the mailbox, keeps "
                     f"it healthy, and Harvey reads its inbox rate here. {AFFILIATE_URL} "
                     f"{AFFILIATE_TAG} Then run: harvey trulyinbox connect")
    if not health.found:
        return Check("Warmup service", FAIL, f"{health.address} isn't in your TrulyInbox "
                     "account yet. Add it in TrulyInbox to start its warmup.")
    if not health.login_ok:
        return Check("Warmup service", FAIL, f"TrulyInbox can't sign in to {health.address}. "
                     "Reconnect the mailbox in TrulyInbox.")
    if not health.warming:
        return Check("Warmup service", FAIL, f"Warmup isn't on for {health.address}. "
                     "Turn it on in TrulyInbox.")
    min_days = warmup.min_days if warmup is not None else 14
    days = (today or date.today()) - health.started_on if health.started_on else None
    days = days.days if days is not None else None
    hint = ""
    if health.started_on and warmup is not None and not warmup.start_date:
        hint = (f" Set channels.email.warmup.start_date: {health.started_on.isoformat()} "
                "so Harvey ramps cold email from it.")
    rate = health.spam_rate
    if rate is not None and health.sent >= MIN_WARMUP_SAMPLE and rate >= MAX_WARMUP_SPAM:
        return Check("Warmup service", FAIL, f"{round(rate * 100)}% spam in warmup over the "
                     "last 14 days. Keep warming and hold cold email until it's under "
                     f"{round(MAX_WARMUP_SPAM * 100)}%." + hint)
    inbox = f", {round((1 - rate) * 100)}% inbox over the last 14 days" if rate is not None else ""
    if health.sent < MIN_WARMUP_SAMPLE:
        return Check("Warmup service", WARN, f"TrulyInbox sent {health.sent} warmup emails in "
                     "the last 14 days: not enough to judge yet. Check that warmup is running "
                     "and keep warming before cold email." + hint)
    if days is None or days < min_days:
        shown = "warming" if days is None else f"day {max(days, 0)} of {min_days}"
        return Check("Warmup service", FAIL, f"TrulyInbox: {shown}{inbox}. Keep warming "
                     "before cold email." + hint)
    return Check("Warmup service", OK, f"TrulyInbox: warmed, day {days}{inbox}. "
                 "Keep it running alongside Harvey." + hint)


def check_send_history(total_sent: int, bounces: int) -> Check:
    if total_sent < MIN_SENDS_FOR_RATES:
        return Check("Bounce rate", INFO, f"{bounces} bounces / {total_sent} sent - too few sends to judge yet.")
    rate = bounces / total_sent
    status = OK if rate <= MAX_BOUNCE_RATE else FAIL
    return Check("Bounce rate", status, f"{rate:.1%} ({bounces}/{total_sent}); keep it under {MAX_BOUNCE_RATE:.0%}.")
