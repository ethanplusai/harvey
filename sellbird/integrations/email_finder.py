"""Email discovery — pattern-first, verify-once, honest about confidence.

Raw SMTP probing is largely dead against Google Workspace and Microsoft
365 (which host most B2B mail): they tarpit probers or accept every
recipient and bounce later. So instead of brute-forcing every name
permutation, this pipeline:

1. Classifies the domain by its MX host (google / microsoft / gateway / other).
2. Derives the domain's email PATTERN once — from cache, then Hunter's
   free domain-search, then known addresses scraped off the site, else a
   sensible default — collapsing N guesses to one candidate.
3. Verifies that single candidate through the best available channel for
   the domain type: a real-verdict API (Reoon, ZeroBounce for the big
   providers), Hunter, or SMTP for small self-hosted servers.
4. Detects catch-all domains and buckets them as ``risky`` instead of
   pretending they're verified.

Every result carries an honest status: verified / risky / guess / invalid.
Only ``verified`` (and optionally ``risky``) should ever be emailed.
"""

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import dns.resolver
import aiosmtplib
import httpx

logger = logging.getLogger("harvey.email_finder")

# Cache MX host + type per domain to avoid repeated lookups.
_mx_cache: dict[str, Optional[str]] = {}

API_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

# Named email-format patterns → builder. {f}=first, {l}=last, {fi}=first initial,
# {li}=last initial. Ordered by real-world prevalence for the default fallback.
PATTERN_BUILDERS = {
    "{f}.{l}": lambda f, l: f"{f}.{l}",
    "{f}{l}": lambda f, l: f"{f}{l}",
    "{fi}{l}": lambda f, l: f"{f[0]}{l}",
    "{f}": lambda f, l: f"{f}",
    "{fi}.{l}": lambda f, l: f"{f[0]}.{l}",
    "{f}_{l}": lambda f, l: f"{f}_{l}",
    "{l}": lambda f, l: f"{l}",
    "{l}.{f}": lambda f, l: f"{l}.{f}",
    "{f}-{l}": lambda f, l: f"{f}-{l}",
    "{l}{fi}": lambda f, l: f"{l}{f[0]}",
}
DEFAULT_PATTERN = "{f}.{l}"
# A business with a handful of people and no evidence of its format: the
# owner of a five-person roofing company is lou@, far more often than
# lou.smith@. Still only a guess — never sent until a verifier confirms it.
SMALL_BUSINESS_PATTERN = "{f}"
# Pattern-cache sources that are only a fallback, not evidence. A cached
# default never blocks a known address or a Hunter lookup, and neither does
# a low-confidence guess from the site's untied addresses: Hunter still gets
# one look. "_after_hunter" marks a domain Hunter has already answered for.
DEFAULT_SOURCES = ("default", "default_after_hunter")
WEAK_SOURCES = DEFAULT_SOURCES + ("site_address_guess", "site_address_guess_after_hunter")


class HunterUnavailable(Exception):
    """Hunter couldn't answer right now (timeout, 429, 5xx). Transient: the
    domain is asked again later, never remembered as having no pattern."""


@dataclass
class EmailResult:
    """Outcome of an email lookup. status is the source of truth."""
    email: str
    status: str          # verified / risky / guess / invalid
    pattern: str = ""
    mx_type: str = ""

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @property
    def sendable(self) -> bool:
        """Safe to send cold: confirmed mailbox only (risky is opt-in elsewhere)."""
        return self.status == "verified"


def _clean_domain(domain: str) -> str:
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.split("/")[0].split("?")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _clean_name_part(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def build_email(pattern: str, first: str, last: str, domain: str) -> str:
    """Render a named pattern into an address, or '' if it can't be built."""
    first = _clean_name_part(first)
    last = _clean_name_part(last)
    domain = _clean_domain(domain)
    if not first or not last or not domain:
        return ""
    builder = PATTERN_BUILDERS.get(pattern)
    if not builder:
        return ""
    try:
        local = builder(first, last)
    except IndexError:
        return ""
    return f"{local}@{domain}" if local else ""


def generate_patterns(first_name: str, last_name: str, domain: str) -> list[str]:
    """All candidate addresses for a person, most-likely first (deduped)."""
    out = []
    for pattern in PATTERN_BUILDERS:
        email = build_email(pattern, first_name, last_name, domain)
        if email:
            out.append(email)
    return list(dict.fromkeys(out))


# ── MX classification ──

def classify_mx(mx_host: str) -> str:
    """Bucket a domain by its mail host — decides the verification strategy."""
    if not mx_host:
        return "none"
    h = mx_host.lower()
    if "google" in h or "googlemail" in h or "aspmx.l" in h:
        return "google"
    if "outlook" in h or "protection.outlook" in h or "microsoft" in h:
        return "microsoft"
    if any(g in h for g in ("pphosted", "mimecast", "barracuda", "proofpoint",
                            "messagelabs", "cisco", "fortinet", "trendmicro")):
        return "gateway"
    return "other"


async def get_mx_host(domain: str) -> Optional[str]:
    """Primary MX host for a domain (cached; None on failure)."""
    domain = _clean_domain(domain)
    if not domain:
        return None
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        loop = asyncio.get_event_loop()
        answers = await loop.run_in_executor(
            None, lambda: dns.resolver.resolve(domain, "MX")
        )
        records = sorted(answers, key=lambda x: x.preference)
        mx_host = str(records[0].exchange).rstrip(".") if records else None
        _mx_cache[domain] = mx_host or None
        return _mx_cache[domain]
    except Exception as e:
        logger.debug(f"MX lookup failed for {domain}: {e}")
        _mx_cache[domain] = None
        return None


# ── Pattern derivation ──

def infer_pattern_from_email(email: str, first: str, last: str) -> Optional[str]:
    """Given one known address, work out which named pattern produced it."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    local = email.split("@", 1)[0]
    first = _clean_name_part(first)
    last = _clean_name_part(last)
    if not first or not last:
        return None
    for pattern, builder in PATTERN_BUILDERS.items():
        try:
            if builder(first, last) == local:
                return pattern
        except IndexError:
            continue
    return None


# Mailboxes that are a function, not a person: they never reveal how the
# business spells its people's addresses.
ROLE_LOCALS = frozenset("""
info office contact contactus hello hi team admin administrator support help sales
service services estimates estimate quotes quote inquiries inquiry enquiries enquiry
billing accounts accounting accountspayable ap ar invoices payments orders order
careers career jobs job hr recruiting employment apply marketing media press pr news
newsletter webmaster web website postmaster hostmaster abuse noreply no-reply donotreply
reception frontdesk front appointments appointment schedule scheduling dispatch
customerservice customercare care clients client mail email general main owner
manager management operations ops legal privacy compliance security it tech
feedback reviews referrals warranty claims insurance parts shop store
""".split())

def pattern_from_local(local: str) -> Optional[tuple[str, float]]:
    """The likely pattern behind one published address when nobody on the
    site is tied to it: corwin@ -> {f}, jsmith@ -> {fi}{l}, j.smith@ ->
    {fi}.{l}, john.smith@ -> {f}.{l}. None for role mailboxes and junk.

    Dotless addresses (corwin@, jsmith@) return None: alone they are
    ambiguous. The confidence is deliberately modest; an address tied to a
    named person (infer_pattern_from_email) is the stronger evidence.
    """
    local = (local or "").strip().lower()
    if not local or local in ROLE_LOCALS or any(ch.isdigit() for ch in local):
        return None
    m = re.fullmatch(r"([a-z]+)([._-])([a-z]+)", local)
    if m:
        first, sep, last = m.groups()
        if sep == "." and len(first) == 1 and len(last) >= 2:
            return "{fi}.{l}", 0.6
        if len(first) >= 2 and len(last) >= 2 and first not in ROLE_LOCALS and last not in ROLE_LOCALS:
            return {".": "{f}.{l}", "_": "{f}_{l}", "-": "{f}-{l}"}[sep], 0.6
        return None
    # A lone dotless address is ambiguous: kmorrow@ may be a first name or
    # K. Morrow, sven@ a first name or S. Ven. Only a named person on the site
    # can tell (see site_email_evidence), so on its own it teaches nothing.
    return None


def _disambiguate(local: str, people) -> Optional[tuple[str, float]]:
    """A dotless address no named person owns, read with the names the site
    does list: jsmith@ where an Amy Smith works is an initial plus a
    surname. Anything else stays ambiguous."""
    if not re.fullmatch(r"[a-z]{3,20}", local or ""):
        return None
    lasts = {_clean_name_part(last) for _, last in people or []} - {""}
    firsts = {_clean_name_part(first) for first, _ in people or []} - {""}
    if local in firsts:
        return "{f}", 0.6
    if any(len(last) >= 3 and local[1:] == last for last in lasts):
        return "{fi}{l}", 0.6
    if local in lasts:
        return "{l}", 0.55
    return None


def site_email_evidence(
    addresses: list[str], people: list[tuple[str, str]], domain: str,
) -> tuple[list[tuple[str, str, str]], str, float]:
    """What a site's own published addresses say about its email format.

    Returns (known, pattern, confidence): ``known`` is every on-domain
    address that belongs to a named person on the site (nvale@ for Nora
    Vale), ready for find_email(known_emails=...). ``pattern`` is the
    format those addresses agree on; failing that, the majority format of
    addresses that show a format without being one person's (j.smith@,
    jsmith@ when John and Jane Smith are both named, or jsmith@ where an Amy
    Smith works); else "". An address two named people could own is never
    returned as either one's.
    """
    domain = _clean_domain(domain)
    known: list[tuple[str, str, str]] = []
    tied_votes: dict[str, int] = {}
    loose_votes: dict[str, int] = {}
    for addr in addresses or []:
        addr = (addr or "").strip().lower()
        local, _, adomain = addr.partition("@")
        if adomain != domain or local in ROLE_LOCALS:
            continue
        owners = []
        for first, last in people or []:
            pattern = infer_pattern_from_email(addr, first, last)
            if pattern:
                owners.append((first, last, pattern))
        if len(owners) == 1:
            first, last, pattern = owners[0]
            known.append((addr, first, last))
            tied_votes[pattern] = tied_votes.get(pattern, 0) + 1
            continue
        if owners:
            # jsmith@ with John and Jane Smith both named: the format is
            # clear, whose mailbox it is isn't. Format evidence only.
            patterns = {o[2] for o in owners}
            if len(patterns) == 1:
                loose_votes[owners[0][2]] = loose_votes.get(owners[0][2], 0) + 2
            continue
        guess = pattern_from_local(local) or _disambiguate(local, people)
        if guess:
            loose_votes[guess[0]] = loose_votes.get(guess[0], 0) + 1
    if tied_votes:
        pattern = max(tied_votes, key=tied_votes.get)
        return known, pattern, 0.95 if tied_votes[pattern] >= 2 else 0.9
    if loose_votes:
        pattern = max(loose_votes, key=loose_votes.get)
        n = loose_votes[pattern]
        return known, pattern, min(0.55 + 0.05 * n, 0.7)
    return known, "", 0.0


async def _hunter_domain_pattern(domain: str, api_key: str) -> Optional[tuple[str, float]]:
    """Hunter's free domain-search returns the org's dominant email pattern.

    Hunter formats it with {first}/{last}/{f} tokens; translate to ours.
    Returns (named_pattern, confidence 0-1), or None when Hunter has no
    pattern for the domain. Raises HunterUnavailable when it couldn't answer.
    """
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": api_key, "limit": 1},
            )
    except httpx.HTTPError as e:
        logger.debug(f"Hunter domain-search failed for {domain}: {e}")
        raise HunterUnavailable(str(e)) from e
    if resp.status_code == 429 or resp.status_code >= 500:
        raise HunterUnavailable(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        return None
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        raise HunterUnavailable("bad JSON")

    hunter_pattern = data.get("pattern") or ""
    translated = _translate_hunter_pattern(hunter_pattern)
    if translated:
        # Hunter's presence of a pattern is a strong signal; scale by how
        # many known emails back it (capped).
        n = len(data.get("emails") or [])
        confidence = min(0.7 + 0.05 * n, 0.95)
        return translated, confidence
    return None


def _translate_hunter_pattern(hunter_pattern: str) -> Optional[str]:
    """Map Hunter's {first}.{last}-style tokens to our named patterns."""
    if not hunter_pattern:
        return None
    p = hunter_pattern.lower().strip()
    mapping = {
        "{first}.{last}": "{f}.{l}",
        "{first}{last}": "{f}{l}",
        "{f}{last}": "{fi}{l}",
        "{first}": "{f}",
        "{f}.{last}": "{fi}.{l}",
        "{first}_{last}": "{f}_{l}",
        "{last}": "{l}",
        "{last}.{first}": "{l}.{f}",
        "{first}-{last}": "{f}-{l}",
        "{last}{f}": "{l}{fi}",
    }
    return mapping.get(p)


def _hunter_api_key() -> str:
    return os.getenv("HUNTER_API_KEY", "").strip()


# Hunter's free tier is 25 domain-searches/month for the whole account, and
# every call spends one no matter how many people it returns. Paid plans set
# HUNTER_MONTHLY_SEARCHES to their own allowance. Every domain-search call in
# Harvey draws from this one counter, so the pattern lookup and Scout's
# named-contact strategy can't each assume they have the full quota.
HUNTER_FREE_MONTHLY_SEARCHES = 25


def _hunter_monthly_cap() -> int:
    raw = os.getenv("HUNTER_MONTHLY_SEARCHES", "").strip()
    if not raw:
        return HUNTER_FREE_MONTHLY_SEARCHES
    try:
        return max(int(raw), 0)
    except ValueError:
        return HUNTER_FREE_MONTHLY_SEARCHES


def _hunter_usage_key() -> str:
    return f"hunter_domain_search_count:{datetime.now(timezone.utc):%Y-%m}"


async def hunter_searches_remaining(state) -> int:
    """Domain-searches left this calendar month (UTC). Without a state
    store there is nothing to count against, so the full cap is reported."""
    cap = _hunter_monthly_cap()
    if state is None:
        return cap
    try:
        used = int(await state.get_setting(_hunter_usage_key(), "0") or "0")
    except (ValueError, TypeError):
        used = 0
    except Exception as e:
        logger.debug(f"Hunter usage read failed: {e}")
        return 0
    return max(cap - used, 0)


async def record_hunter_search(state) -> None:
    """Count one domain-search against this month's budget. Called before
    the request, so a call that fails mid-flight is still counted."""
    if state is None:
        return
    try:
        await state.increment_setting(_hunter_usage_key())
    except Exception as e:
        logger.debug(f"Hunter usage write failed: {e}")


async def hunter_domain_people(
    domain: str, api_key: str, limit: int = 10
) -> Optional[list[dict]]:
    """Hunter's domain-search doesn't just give a pattern — it lists real
    named people it already has on file for the domain (name, title, email,
    and its own verification verdict). Returns the raw ``emails`` list, or
    None on any failure (network, non-200, bad JSON, or no data).

    Costs one domain-search credit per call regardless of ``limit``, so
    callers must check hunter_searches_remaining() and record_hunter_search()
    first rather than rely on this failing gracefully to protect the quota.
    """
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": api_key, "limit": limit},
            )
    except httpx.HTTPError as e:
        logger.debug(f"Hunter domain-search failed for {domain}: {e}")
        return None
    if resp.status_code != 200:
        return None
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        return None
    emails = data.get("emails")
    return emails if isinstance(emails, list) else None


async def derive_pattern(
    domain: str,
    known_emails: Optional[list[tuple[str, str, str]]] = None,
    state=None,
    default_pattern: str = DEFAULT_PATTERN,
) -> tuple[str, str, float]:
    """Determine a domain's email pattern. Returns (pattern, source, confidence).

    Order: a known address for this domain (a named person's address on
    the site) → the DB cache, unless all it holds is a default → Hunter
    domain-search (once per domain) → ``default_pattern``. This is the step
    that collapses six guesses to one and is where free tiers stretch
    furthest. A cached default is a fallback, not evidence: it never blocks
    a real address or Hunter (it used to, forever).
    """
    domain = _clean_domain(domain)

    # A confirmed address for this domain reveals the pattern for free.
    for email, first, last in known_emails or []:
        if email.split("@")[-1].lower() == domain:
            inferred = infer_pattern_from_email(email, first, last)
            if inferred:
                return inferred, "known_email", 0.9

    cached = None
    if state is not None:
        try:
            cached = await state.get_email_pattern(domain)
        except Exception as e:
            logger.debug(f"Pattern cache read failed for {domain}: {e}")
    source = (cached or {}).get("source") or ""
    if cached and cached.get("pattern") and source not in WEAK_SOURCES:
        return cached["pattern"], source or "cache", cached.get("confidence", 0.6)

    # A site guess is better than nothing, but Hunter gets one look first.
    site_guess = cached if cached and cached.get("pattern") and source.startswith("site_address_guess") else None
    hunter_tried = source.endswith("_after_hunter")
    key = _hunter_api_key()
    if key and not hunter_tried and await hunter_searches_remaining(state) > 0:
        await record_hunter_search(state)
        try:
            result = await _hunter_domain_pattern(domain, key)
            answered = True
        except HunterUnavailable as e:
            # Transient: not remembered, so it's asked again next time.
            logger.debug(f"Hunter unavailable for {domain}: {e}")
            result, answered = None, False
        if result:
            return result[0], "hunter_domain_search", result[1]
        if answered:
            # Remembered, so the monthly budget isn't spent on this domain again.
            if site_guess:
                return site_guess["pattern"], "site_address_guess_after_hunter", site_guess.get("confidence", 0.6)
            return default_pattern, "default_after_hunter", 0.3

    if site_guess:
        return site_guess["pattern"], source, site_guess.get("confidence", 0.6)
    return default_pattern, source if hunter_tried else "default", 0.3


# ── Verification channels ──

async def verify_reoon(email: str, api_key: str) -> Optional[dict]:
    """Reoon email-verifier — generous free tier, good catch-all flagging.

    Returns {'status': ..., 'catch_all': bool} or None if unavailable.
    Reoon power-mode 'status' values: safe / invalid / disabled / disposable /
    inbox_full / catch_all / role_account / spamtrap / unknown. 'safe' is its
    deliverable verdict; a 'role_account' (info@, office@) counts as valid
    only when Reoon also reports it deliverable.
    """
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.get(
                "https://emailverifier.reoon.com/api/v1/verify",
                params={"email": email, "key": api_key, "mode": "power"},
            )
    except httpx.HTTPError as e:
        logger.debug(f"Reoon request failed for {email}: {e}")
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    status = str(data.get("status") or "").lower()
    if status == "role_account":
        status = "valid" if data.get("is_deliverable") is True else "unknown"
    is_catch_all = bool(data.get("is_catch_all") or status == "catch_all")
    return {"status": status, "catch_all": is_catch_all}


async def verify_zerobounce(email: str, api_key: str) -> Optional[dict]:
    """ZeroBounce — its 2026 engine is the reliable one for M365/Workspace catch-alls."""
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.get(
                "https://api.zerobounce.net/v2/validate",
                params={"email": email, "api_key": api_key},
            )
    except httpx.HTTPError as e:
        logger.debug(f"ZeroBounce request failed for {email}: {e}")
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    status = str(data.get("status") or "").lower()
    sub = str(data.get("sub_status") or "").lower()
    return {"status": status, "catch_all": sub == "catch-all" or status == "catch-all"}


async def verify_hunter(email: str, api_key: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/email-verifier",
                params={"email": email, "api_key": api_key},
            )
    except httpx.HTTPError as e:
        logger.debug(f"Hunter verify failed for {email}: {e}")
        return None
    if resp.status_code != 200:
        return None
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        return None
    status = str(data.get("status") or data.get("result") or "").lower()
    return {"status": status, "catch_all": bool(data.get("accept_all"))}


async def verify_email_smtp(email: str) -> Optional[bool]:
    """SMTP RCPT check — only trustworthy for small, self-hosted mail servers.

    Returns True (accepted), False (rejected), or None (couldn't tell).
    Port 25 is blocked on most cloud hosts, so this frequently returns None.
    """
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    mx_host = await get_mx_host(domain)
    if not mx_host:
        return None
    smtp = aiosmtplib.SMTP(hostname=mx_host, port=25, timeout=10)
    try:
        await smtp.connect()
        await smtp.ehlo()
        await smtp.mail("verify@example.com")
        code, _ = await smtp.rcpt(email)
        if code == 250:
            return True
        if code in (550, 551, 553):
            return False
        return None
    except Exception as e:
        logger.debug(f"SMTP verification failed for {email}: {e}")
        return None
    finally:
        try:
            await smtp.quit()
        except Exception:
            pass


async def detect_catch_all(domain: str) -> Optional[bool]:
    """Probe a definitely-fake address; if accepted, the domain is catch-all.

    Only works where SMTP probing works at all (small hosts). Returns None
    when we can't tell.
    """
    fake = f"zz-no-such-user-{random.randint(10000, 99999)}@{_clean_domain(domain)}"
    result = await verify_email_smtp(fake)
    if result is True:
        return True   # accepted a fake address → catch-all
    if result is False:
        return False  # rejected a fake address → not catch-all
    return None


def _status_from_verdict(verdict: dict) -> Optional[str]:
    """Map a provider verdict dict to our status, or None if inconclusive."""
    if verdict.get("catch_all"):
        return "risky"
    status = verdict.get("status", "")
    if status in ("valid", "deliverable", "safe"):
        return "verified"
    if status in ("invalid", "undeliverable", "disabled", "disposable", "spamtrap"):
        return "invalid"
    if status in ("catch_all", "catch-all", "accept_all"):
        return "risky"
    return None  # unknown / risky-unknown — inconclusive


async def _verify_candidate(email: str, mx_type: str, env) -> tuple[Optional[str], bool]:
    """Run the best available verifier for this domain type.

    Returns (status_or_None, catch_all_seen). Order is chosen so scarce
    paid-ish credits are spent only where they actually resolve the domain.
    """
    reoon_key = getattr(env, "reoon_api_key", "") if env else ""
    zerobounce_key = getattr(env, "zerobounce_api_key", "") if env else ""
    hunter_key = _hunter_api_key()

    catch_all_seen = False

    # Big providers: ZeroBounce first (its engine cracks their catch-alls),
    # then Reoon, then Hunter. SMTP is useless/misleading here.
    if mx_type in ("google", "microsoft", "gateway"):
        channels = []
        if zerobounce_key:
            channels.append(lambda: verify_zerobounce(email, zerobounce_key))
        if reoon_key:
            channels.append(lambda: verify_reoon(email, reoon_key))
        if hunter_key:
            channels.append(lambda: verify_hunter(email, hunter_key))
        for channel in channels:
            verdict = await channel()
            if verdict is None:
                continue
            if verdict.get("catch_all"):
                catch_all_seen = True
            status = _status_from_verdict(verdict)
            if status is not None:
                return status, catch_all_seen
        return None, catch_all_seen

    # Small / self-hosted: Reoon → SMTP (still works here) → Hunter.
    if reoon_key:
        verdict = await verify_reoon(email, reoon_key)
        if verdict:
            if verdict.get("catch_all"):
                catch_all_seen = True
            status = _status_from_verdict(verdict)
            if status is not None:
                return status, catch_all_seen

    smtp = await verify_email_smtp(email)
    if smtp is True:
        # Confirm it's not just a catch-all before trusting "verified".
        if await detect_catch_all(email.split("@")[1]) is True:
            return "risky", True
        return "verified", catch_all_seen
    if smtp is False:
        return "invalid", catch_all_seen

    if hunter_key:
        verdict = await verify_hunter(email, hunter_key)
        if verdict:
            if verdict.get("catch_all"):
                catch_all_seen = True
            status = _status_from_verdict(verdict)
            if status is not None:
                return status, catch_all_seen

    return None, catch_all_seen


async def verify_published_address(email: str, env=None) -> str:
    """Grade an address the company publishes on its own website.

    Graded like any other address: a catch-all verdict stays "risky", so
    ``send_to_risky`` still decides whether it is sent (a published address
    can be stale). A verifier's invalid verdict wins, and an inconclusive
    check stays an honest guess that is never sent.
    """
    email = (email or "").strip().lower()
    domain = email.partition("@")[2]
    if not domain:
        return "invalid"
    mx_host = await get_mx_host(domain)
    if not mx_host:
        return "invalid"
    status, _ = await _verify_candidate(email, classify_mx(mx_host), env)
    return status or "guess"


def _belongs_to_someone_else(candidate, pattern, person, domain, other_people, known_emails) -> bool:
    me = (_clean_name_part(person[0]), _clean_name_part(person[1]))
    for email, first, last in known_emails or []:
        if email.strip().lower() == candidate and (_clean_name_part(first), _clean_name_part(last)) != me:
            return True
    for first, last in other_people or []:
        if (_clean_name_part(first), _clean_name_part(last)) == me:
            continue
        if build_email(pattern, first, last, domain) == candidate:
            return True
    return False


# ── Top-level entry point ──

async def find_email(
    first_name: str,
    last_name: str,
    domain: str,
    verify: bool = True,
    env=None,
    state=None,
    known_emails: Optional[list[tuple[str, str, str]]] = None,
    default_pattern: str = DEFAULT_PATTERN,
    other_people: Optional[list[tuple[str, str]]] = None,
) -> EmailResult:
    """Find and honestly grade one email for a person at a company.

    Returns an EmailResult whose ``status`` is verified / risky / guess /
    invalid. When ``verify`` is False (or no verifier is configured), the
    best pattern guess is returned as ``guess`` — never as verified. A
    person's own published address (in ``known_emails``) is used as is,
    and is still graded like any other.

    ``other_people`` are the other people named at the company. A candidate
    one of them could own (jsmith@ for Jane when John Smith works there too,
    or an address published as theirs) is never guessed or verified for
    this person: a verifier would confirm the other person's mailbox.
    """
    domain = _clean_domain(domain)
    if not (first_name and last_name and domain):
        return EmailResult(email="", status="invalid")

    mx_host = await get_mx_host(domain)
    if not mx_host:
        # No MX → cannot receive mail at all.
        guess = build_email(DEFAULT_PATTERN, first_name, last_name, domain)
        return EmailResult(email=guess, status="invalid", mx_type="none")

    mx_type = classify_mx(mx_host)

    pattern, source, confidence = await derive_pattern(
        domain, known_emails=known_emails, state=state, default_pattern=default_pattern,
    )
    candidate = build_email(pattern, first_name, last_name, domain)
    own = _clean_name_part(first_name), _clean_name_part(last_name)
    for email, first, last in known_emails or []:
        if (_clean_name_part(first), _clean_name_part(last)) == own \
                and email.split("@")[-1].lower() == domain:
            candidate = email.strip().lower()
            pattern = infer_pattern_from_email(candidate, first, last) or pattern
            break
    if _belongs_to_someone_else(candidate, pattern, (first_name, last_name), domain,
                                other_people, known_emails):
        logger.info(f"Email for {first_name} {last_name}@{domain}: {candidate} could be "
                    "another named person's mailbox; not guessed")
        return EmailResult(email="", status="guess", pattern=pattern, mx_type=mx_type)
    if not candidate:
        candidate = build_email(DEFAULT_PATTERN, first_name, last_name, domain)
        pattern = DEFAULT_PATTERN

    if state is not None:
        try:
            await state.save_email_pattern(
                domain, pattern=pattern, source=source,
                confidence=confidence, mx_type=mx_type,
            )
        except Exception as e:
            logger.debug(f"Pattern cache write failed for {domain}: {e}")

    if not verify:
        return EmailResult(email=candidate, status="guess", pattern=pattern, mx_type=mx_type)

    status, catch_all = await _verify_candidate(candidate, mx_type, env)

    if catch_all and state is not None:
        try:
            await state.save_email_pattern(domain, mx_type=mx_type, is_catch_all=1)
        except Exception:
            pass

    if status is None:
        # Verification couldn't decide — an honest guess, never "verified".
        status = "guess"

    logger.info(f"Email for {first_name} {last_name}@{domain}: {candidate} [{status}]")
    return EmailResult(email=candidate, status=status, pattern=pattern, mx_type=mx_type)
