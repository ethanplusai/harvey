"""Optional per-recipient cold-email check through Precleared (precleared.dev).

Off by default. With ``compliance.precleared.enabled: true``, the sender asks
Precleared about each new recipient before the first cold email of a
sequence: may a cold marketing email go to this address under UK PECR, US
CAN-SPAM, Canada's CASL, Germany's UWG or France's rules? Anything other than
``ok_to_email`` holds the email back with the reason, and the signed receipt
id is logged with the send so the decision can be shown later.

One check per address: a definite verdict is cached in the settings table for
CACHE_DAYS, together with a fingerprint of the facts it was decided on (sender
country, address source, product description and, if set, the recipient's or
the prospects' country and the company's and person's names). A cached verdict
is reused only while those facts are unchanged, so a three-step sequence costs
one check but a config change forces a fresh one. Replies are never checked.
Precleared receives the recipient's address, your sender country, the address
source you configure, your product description (used to judge relevance under
CASL and French rules) and the recipient's country when Harvey is sure of it
from the company's location (``send_window.recipient_country``), which
Precleared takes as settled. Otherwise it receives, if there is one, your
prospects' country (the configured ``prospect_country``, or the one country
every ``icp.geography`` entry is in), which it uses only when it can't work out
the recipient's country from the address, company records or the company's
website. It also receives the company's name and the person's full name when
Harvey has them (a company inbox has no person, so no name). For a UK
recipient Precleared uses them to find the company on the Companies House
register when the domain alone doesn't, and it doesn't store the person's
name.

A response Precleared can't vouch for (network trouble, daily allowance used
up, a bad key, or a malformed body) holds cold email for a later cycle instead
of failing it. When the failure is about Precleared rather than the address
(no connection or no answer in time, a busy server, the allowance or the rate
limit, the key, an answer Harvey can't read), no more fresh checks are made
that cycle, but a stored verdict is still used, so email to people already
cleared carries on. Any other failure moves just that address's email back
(see the sender's ADDRESS_RETRY_AFTER) and the rest of the cycle carries on,
unless addresses at different domains fail with no answer in between, which
pauses checks too. When the check timed out or Precleared's answer couldn't be
read, that email is moved back as well, in case the address is the cause (a
slow lookup, an odd answer), so one address can't keep every other one
waiting.

Without an API key Precleared allows 10 checks a day; a free key
(https://precleared.dev/get-key, PRECLEARED_API_KEY) allows 25 a day.
Beyond that, a key draws on a prepaid pack. When a key runs out, the held
reason gives the checkout link from Precleared's answer.

A hold is also recorded under STATUS_KEY, so the dashboard's Today page and
`harvey status` say that cold email is waiting and how to fix it (a free
key, a pack, or nothing but time). The next fresh answer clears it.
"""

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import httpx

from harvey.config import normalize_prospect_country
from harvey.send_window import icp_country

logger = logging.getLogger("harvey.precleared")

API_URL = "https://precleared.dev/api/v1/check"
# Precleared answers within 25 seconds (with verdict "unknown" if a register
# lookup is slow), so the read timeout sits just above that.
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
# httpx's read timeout applies to each chunk, so an answer that trickles in
# could outlast it; this bounds the whole request.
TOTAL_TIMEOUT = 40.0
CACHE_DAYS = 30
# Bump when the facts in _fingerprint change, so older cache entries are
# ignored rather than reused.
CACHE_VERSION = 2
SETTINGS_PREFIX = "precleared:"
VERDICTS = {"ok_to_email", "consent_needed", "blocked", "unknown"}
DEFINITE_VERDICTS = {"ok_to_email", "consent_needed", "blocked"}
ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")
SENDER_COUNTRIES = ("GB", "US")
# Precleared's limits for the company and person names sent with a check.
# A longer value would be refused, so names are cut to fit.
MAX_COMPANY_NAME = 160
MAX_RECIPIENT_NAME = 120
# Where to buy more checks when a 402 answer carries no usable checkout link.
BUY_URL = "https://precleared.dev/buy"
GET_KEY_URL = "https://precleared.dev/get-key"
MAX_BUY_URL = 2048
# What `harvey status` and the dashboard's Today page read about the check:
# {"status": "held", "kind", "reason", "url", "waiting", "since", "at"}, or
# "" when the last fresh call got an answer. Written by the sender.
STATUS_KEY = "precleared_status"
# Precleared's allowance resets daily, so a hold not seen for this long is
# stale: Harvey stopped running, or the check was switched off.
STATUS_MAX_AGE = timedelta(hours=26)
HOLD_KINDS = ("keyless_limit", "out_of_checks", "key_rejected", "rate_limited", "unavailable")
# A 402's checkout link is only followed to Precleared itself or its card
# checkout; any other host falls back to BUY_URL.
BUY_HOSTS = ("precleared.dev", "checkout.stripe.com")
# Answers that mean Precleared's server is busy or down, not that it can't
# check one address.
SERVER_BUSY = (502, 503, 504)


@dataclass
class Clearance:
    """Precleared's answer for one address.

    ``allowed``: send it. ``held``: the check could not be made right now
    (network trouble, daily allowance used up, bad key, malformed response),
    so the email stays queued and is tried again on a later cycle.
    ``pause_checks``: that failure is about Precleared rather than this
    address, so no more fresh checks are made this cycle. ``retry_address``:
    the check timed out or its answer couldn't be read, which this address
    may have caused, so its email is moved back too. Otherwise the email is
    blocked and ``reason`` says why.
    """

    allowed: bool
    held: bool = False
    pause_checks: bool = False
    retry_address: bool = False
    verdict: str = ""
    reason: str = ""
    receipt_id: str = ""
    # The recipient's country as Precleared worked it out ("GB"), or "".
    # The send window uses it when nothing else tells the recipient's zone.
    country: str = ""
    # Holds only: why the check couldn't be made (one of HOLD_KINDS) and
    # where the user can fix it (a free key or this key's checkout link).
    hold_kind: str = ""
    fix_url: str = ""
    # True when the verdict came from the 30-day cache rather than a call.
    cached: bool = False


class PreclearedClient:
    def __init__(
        self,
        sender_country: str,
        api_key: str = "",
        address_source: str = "unknown",
        pitch: str = "",
        prospect_country: str = "",
        default_country: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.sender_country = sender_country
        self.api_key = api_key or ""
        self.address_source = address_source
        self.pitch = (pitch or "").strip()[:200]
        self.prospect_country = normalize_prospect_country(prospect_country)
        # The country sent as the default when nothing names the recipient's:
        # prospect_country, else default_country (from_config passes the one
        # country the ICP geography is in).
        self.default_country = self.prospect_country or normalize_prospect_country(default_country)
        self._transport = transport  # tests inject httpx.MockTransport

    @classmethod
    def from_config(cls, config, env) -> "PreclearedClient | None":
        """Build a client when the check is switched on, else return None."""
        compliance = getattr(config, "compliance", None)
        settings = getattr(compliance, "precleared", None)
        if settings is None or getattr(settings, "enabled", False) is not True:
            return None
        product = getattr(config, "product", None)
        return cls(
            sender_country=settings.sender_country,
            api_key=getattr(env, "precleared_api_key", ""),
            address_source=settings.address_source,
            pitch=getattr(product, "description", ""),
            prospect_country=getattr(settings, "prospect_country", ""),
            default_country=icp_country(config),
        )

    def _redact(self, text: str) -> str:
        if self.api_key and text:
            text = text.replace(self.api_key, "***REDACTED***")
            # Echoed escaped or percent-encoded: drop the text altogether.
            if _contains_key(text, self.api_key):
                return "***REDACTED***"
        return text or ""

    def _fingerprint(self, country: str = "", company: str = "", name: str = "") -> str:
        """Hash of the facts Precleared decides on, apart from the address.

        These are exactly the fields _check sends with the address, so a
        cached verdict is only reused for the same question. ``country`` is
        the recipient's country when Harvey is sure of it, and ``company``
        and ``name`` the company's and person's names, as for _check.
        """
        facts = {
            "v": CACHE_VERSION,
            "sender_country": self.sender_country,
            "address_source": self.address_source,
            "pitch": self.pitch,
            "existing_customer": False,
        }
        if country:
            # Only when set, so a verdict cached without one still matches
            # while the address has none (and see _cached once it has).
            facts["recipient_country"] = country
        elif self.prospect_country:
            # Only when set, so cached verdicts from before the setting existed
            # still match.
            facts["prospect_country"] = self.prospect_country
        elif self.default_country:
            # The ICP's country stands in for prospect_country; only while it
            # does, for the same reason.
            facts["icp_country"] = self.default_country
        if company:
            # Only when Harvey has them, so a verdict cached without the names
            # still matches while an address has none (and see _cached once
            # it has).
            facts["company_name"] = company
        if name:
            facts["recipient_name"] = name
        return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()

    async def clear(self, to_email: str, state, country: str = "",
                    company: str = "", name: str = "") -> Clearance:
        """Return the verdict for one address, from the cache when possible."""
        cached = await self.cached(to_email, state, country, company=company, name=name)
        return cached if cached is not None else await self.check(to_email, state, country, company=company, name=name)

    async def cached(self, to_email: str, state, country: str = "",
                    company: str = "", name: str = "") -> Clearance | None:
        """The stored verdict for this address and these facts, or None."""
        email = (to_email or "").strip().lower()
        return self._cached(await state.get_setting(SETTINGS_PREFIX + email), _country(country),
                            _name(company, MAX_COMPANY_NAME), _name(name, MAX_RECIPIENT_NAME))

    def stored_verdicts(self) -> tuple[str, str, str]:
        """What a stored verdict needs to still be used, for counting in SQL:
        its settings key prefix, these facts' fingerprint, and the oldest
        ``checked_at`` that hasn't expired."""
        oldest = (_utcnow() - timedelta(days=CACHE_DAYS)).isoformat()
        return SETTINGS_PREFIX, self._fingerprint(), oldest

    async def check(self, to_email: str, state, country: str = "",
                    company: str = "", name: str = "") -> Clearance:
        """Ask Precleared now, and store a definite verdict for next time."""
        email = (to_email or "").strip().lower()
        company = _name(company, MAX_COMPANY_NAME)
        name = _name(name, MAX_RECIPIENT_NAME)
        try:
            result = await self._check(email, _country(country), company, name)
        except Exception as e:
            # A failure nobody planned for holds cold email and pauses checks
            # (it may well happen for every address); it never stops the cycle.
            return Clearance(allowed=False, held=True, pause_checks=True, retry_address=True,
                             hold_kind="unavailable",
                             reason=_clean(self._redact(
                                 f"Precleared check failed ({type(e).__name__}: {e})"))[:240])
        if not result.held and result.verdict in DEFINITE_VERDICTS:
            # One entry per address: a fresh verdict replaces any earlier
            # one, including legacy entries written without a fingerprint.
            await state.set_setting(SETTINGS_PREFIX + email, json.dumps({
                "verdict": result.verdict,
                "reason": result.reason,
                "receipt_id": result.receipt_id,
                "country": result.country,
                "checked_at": _utcnow().isoformat(),
                "inputs": self._fingerprint(_country(country), company, name),
            }))
        return result

    async def known_country(self, to_email: str, state, country: str = "",
                            company: str = "", name: str = "") -> str:
        """The recipient's country from a cached verdict, or "". Never asks
        Precleared. ``country``, ``company`` and ``name`` are what clear()
        was given for this address, since the verdict is keyed on them."""
        email = (to_email or "").strip().lower()
        cached = self._cached(await state.get_setting(SETTINGS_PREFIX + email), _country(country),
                              _name(company, MAX_COMPANY_NAME), _name(name, MAX_RECIPIENT_NAME))
        return cached.country if cached is not None else ""

    def _cached(self, raw: str, country: str, company: str = "", name: str = "") -> Clearance | None:
        """The cached verdict for the question _check would ask, or None.

        A verdict decided without the names is kept when it already allows
        the email, since the names only help Precleared find the company;
        any other verdict is asked for again once, with them.
        """
        cached = self._same_question(raw, country, company, name)
        if cached is None and (company or name):
            earlier = self._same_question(raw, country)
            if earlier is not None and earlier.verdict == "ok_to_email":
                return earlier
        return cached

    def _same_question(self, raw: str, country: str, company: str = "",
                       name: str = "") -> Clearance | None:
        """The verdict cached for exactly these facts, or None.

        A verdict decided without the country, for which Precleared found
        that same country itself, answers the same question, so it is kept
        rather than asked for again.
        """
        cached = _from_cache(raw, self._fingerprint(country, company, name))
        if cached is None and country:
            earlier = _from_cache(raw, self._fingerprint("", company, name))
            if earlier is not None and earlier.country == country:
                return earlier
        return cached

    async def _check(self, email: str, country: str = "", company: str = "",
                     name: str = "") -> Clearance:
        payload = {
            "recipient_email": email,
            "sender_country": self.sender_country,
            "address_source": self.address_source,
            "existing_customer": False,
        }
        if self.pitch:
            payload["pitch"] = self.pitch
        if country:
            # Harvey is sure of it from the company's location, and Precleared
            # takes it as settled; so never a guess, and never both fields.
            payload["recipient_country"] = country
        elif self.default_country:
            # Precleared uses this only when the address, company records and
            # the company's website don't show the recipient's country.
            payload["default_recipient_country"] = self.default_country
        if company:
            # For a UK recipient, searched on the Companies House register
            # when the domain alone doesn't find the company. A company found
            # this way only counts when other evidence ties it to the
            # recipient, such as one of its officers being the person below.
            payload["company_name"] = company
        if name:
            # Matched against the company's current officers. Precleared
            # doesn't store it.
            payload["recipient_name"] = name
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "harvey (+https://github.com/ethanplusai/harvey)",
        }
        if self.api_key:
            if not re.fullmatch(r"[!-~]+", self.api_key):
                # A key is plain printable ASCII. One pasted with a curly
                # quote, a space or a line break can't be sent as a header,
                # and the error would print it back, so it isn't tried.
                return Clearance(allowed=False, held=True, pause_checks=True,
                                 hold_kind="key_rejected", fix_url=GET_KEY_URL,
                                 reason="PRECLEARED_API_KEY has a character that can't be "
                                        "sent (a curly quote, a space or a line break); "
                                        "check it in .env")
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                resp = await asyncio.wait_for(
                    client.post(API_URL, json=payload, headers=headers), TOTAL_TIMEOUT)
        except httpx.HTTPError as e:
            # No answer in time may be this address (a slow lookup); no
            # connection, or one dropped (a restart), is the same for everyone.
            slow = isinstance(e, httpx.ReadTimeout)
            return Clearance(allowed=False, held=True, pause_checks=True, retry_address=slow,
                             hold_kind="unavailable",
                             reason=self._redact(f"Precleared unreachable: {e}"))
        except asyncio.TimeoutError:
            return Clearance(allowed=False, held=True, pause_checks=True, retry_address=True,
                             hold_kind="unavailable",
                             reason="Precleared took too long to answer")

        if resp.status_code == 429:
            if self.api_key or _error_code(resp) == "rate_limited":
                # A key that runs out of checks gets a 402, so a 429 on a key
                # is Precleared's per-minute rate limit, not the daily one.
                return Clearance(allowed=False, held=True, pause_checks=True,
                                 hold_kind="rate_limited",
                                 reason="Precleared rate limit reached; asking again next cycle")
            # Keyless allowance used up: a free key gives this install its own.
            return Clearance(
                allowed=False, held=True, pause_checks=True,
                hold_kind="keyless_limit", fix_url=GET_KEY_URL,
                reason="Precleared daily allowance used up; add PRECLEARED_API_KEY "
                       "(free key: https://precleared.dev/get-key) or wait until tomorrow",
            )
        if resp.status_code == 402:
            # The key's free checks for today are used and it has no credits left,
            # so another key won't help: a pack (or tomorrow's allowance) will.
            # The 402 answer carries a checkout link for this key.
            buy_url = _buy_url(resp, self.api_key)
            return Clearance(
                allowed=False, held=True, pause_checks=True,
                hold_kind="out_of_checks", fix_url=buy_url,
                reason=self._redact(
                    "Precleared: today's free checks on your key are used and it has "
                    f"no credits left; add a pack ({buy_url}) "
                    "or wait until tomorrow"
                ),
            )
        if resp.status_code == 401:
            return Clearance(allowed=False, held=True, pause_checks=True,
                             hold_kind="key_rejected", fix_url=GET_KEY_URL,
                             reason="PRECLEARED_API_KEY was rejected (wrong or revoked)")
        if resp.status_code != 200:
            # A busy or unreachable server affects every address; anything
            # else may be about this address alone, so only its email waits.
            return Clearance(allowed=False, held=True,
                             pause_checks=resp.status_code in SERVER_BUSY, hold_kind="unavailable",
                             reason=f"Precleared HTTP {resp.status_code}: "
                                    f"{_clean(self._redact(resp.text))[:200]}")

        try:
            data = resp.json()
        except (ValueError, RecursionError):
            return Clearance(allowed=False, held=True, pause_checks=True, retry_address=True,
                             hold_kind="unavailable", reason="Precleared returned invalid JSON")

        # Only a JSON object with a known verdict counts as an answer.
        # Anything else is treated like an unavailable check, so cold email
        # is held, not failed. A 200 means the check was made (and counted),
        # and an answer Harvey can't read is about the API, not the address,
        # so no more checks are spent on it this cycle.
        if not isinstance(data, dict):
            return Clearance(allowed=False, held=True, pause_checks=True, retry_address=True,
                             hold_kind="unavailable",
                             reason="Precleared returned an unexpected response (not a JSON object)")
        verdict = data.get("verdict")
        if not isinstance(verdict, str) or verdict not in VERDICTS:
            return Clearance(allowed=False, held=True, pause_checks=True, retry_address=True,
                             hold_kind="unavailable",
                             reason="Precleared returned a response without a known verdict")

        receipt = data.get("receipt")
        receipt_id = _text(receipt.get("id")) if isinstance(receipt, dict) else ""
        facts = data.get("facts")
        country = _country(facts.get("recipient_country")) if isinstance(facts, dict) else ""
        if verdict == "ok_to_email":
            return Clearance(allowed=True, verdict=verdict, receipt_id=receipt_id,
                             country=country)
        return Clearance(
            allowed=False,
            verdict=verdict,
            reason=_clean(_reason(data)),
            receipt_id=receipt_id,
            country=country,
        )


def _name(value, limit: int) -> str:
    """A company or person name for the check, with runs of whitespace made
    single spaces and cut to Precleared's limit, or "" for anything else."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def _country(value) -> str:
    """A two-letter country code, from Precleared's facts or the sender, or ""."""
    code = value.strip().upper() if isinstance(value, str) else ""
    return code if len(code) == 2 and code.isascii() and code.isalpha() else ""


def _text(value) -> str:
    """A stripped string, or "" for anything that isn't one."""
    return value.strip() if isinstance(value, str) else ""


def _clean(text: str) -> str:
    """Text with control characters (C0 incl. ESC, DEL, C1) made spaces,
    so text Precleared supplied can't drive a terminal (links, clears)."""
    return "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text or "")


def _contains_key(text: str, api_key: str) -> bool:
    """Whether ``text`` holds ``api_key`` as is, JSON-escaped (``\\/``) or
    percent-encoded (any case, nested). Too deep to finish checking counts
    as yes."""
    decoded = text
    for _ in range(8):
        if api_key in decoded:
            return True
        next_decoded = unquote(decoded).replace("\\/", "/")
        if next_decoded == decoded:
            return False
        decoded = next_decoded
    return True


def _error_code(resp: httpx.Response) -> str:
    """The ``error`` field of a JSON error body, or ""."""
    try:
        data = resp.json()
    except (ValueError, RecursionError):
        return ""
    return _text(data.get("error")) if isinstance(data, dict) else ""


def _buy_url(resp: httpx.Response, api_key: str = "") -> str:
    """The checkout link from a 402 answer's ``buy_url``, or BUY_URL.

    Only a plain https link of sane length is used; a missing, malformed or
    non-JSON body falls back to Precleared's buy page.
    """
    try:
        data = resp.json()
    except (ValueError, RecursionError):
        return BUY_URL
    url = data.get("buy_url") if isinstance(data, dict) else None
    if not (
        isinstance(url, str)
        and len(url) <= MAX_BUY_URL
        and url.isprintable()
        and not any(ch.isspace() for ch in url)
        and "\\" not in url
    ):
        return BUY_URL
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port  # Access validates numeric syntax and range.
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or "%" in parsed.netloc
                or parsed.netloc.endswith(":") or port == 0):
            return BUY_URL
        if ":" in host:
            ipaddress.IPv6Address(host)
            # urlsplit accepts suffixes after the closing bracket; a browser
            # need not interpret that malformed authority the same way.
            if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parsed.netloc):
                return BUY_URL
        else:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
            if len(ascii_host) > 253 or not all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                for label in ascii_host.split(".")
            ):
                return BUY_URL
        if not any(host == h or host.endswith("." + h) for h in BUY_HOSTS):
            return BUY_URL
    except (ValueError, UnicodeError):
        return BUY_URL

    if api_key and _contains_key(url, api_key):
        # Decoded copies are inspected, but a usable checkout/session URL
        # is kept intact.
        return BUY_URL
    return url


def _reason(data: dict) -> str:
    """One readable line: the rule that decided it (or the note), then the next step.

    Precleared's ``reasons`` lead with the matching rule as
    ``{"rule_key", "text", "source_title", "source_url"}``, or with a
    ``{"note": ...}`` when no rule could be applied. Fields with an
    unexpected shape are skipped.
    """
    reasons = data.get("reasons")
    first = reasons[0] if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict) else {}
    text = _text(first.get("text")) or _text(first.get("note"))
    source = _text(first.get("source_title"))
    parts = []
    if text:
        parts.append(f"{text} ({source})" if source else text)
    next_step = _text(data.get("next_step"))
    if next_step:
        parts.append(next_step)
    return " ".join(parts)[:240]


def _from_cache(raw: str, inputs: str) -> Clearance | None:
    """A cached verdict, or None when there is none to trust.

    Entries decided on different facts, legacy entries without a
    fingerprint, expired or malformed entries all count as missing.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("inputs") != inputs:
        return None
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict not in DEFINITE_VERDICTS:
        return None
    try:
        checked_at = datetime.fromisoformat(str(data["checked_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if _utcnow() - checked_at > timedelta(days=CACHE_DAYS):
        return None
    return Clearance(
        allowed=verdict == "ok_to_email",
        verdict=verdict,
        reason=_text(data.get("reason")),
        receipt_id=_text(data.get("receipt_id")),
        country=_country(data.get("country")),
        cached=True,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Holds: what `harvey status` and the Today page show ──


def parse_status(raw: str) -> dict:
    """The stored STATUS_KEY value as a dict ({} if absent or bad)."""
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def record_hold(state, clearance: Clearance, waiting: int) -> None:
    """Store that cold email is waiting on the check, and how to fix it.

    ``since`` is kept while the same kind of hold continues, so the user
    can see how long cold email has been waiting; ``at`` is the last time
    a cycle hit it.
    """
    try:
        # Only a hold still current carries its ``since`` over; one not seen
        # for STATUS_MAX_AGE is over, and this starts a new one.
        previous = await current_hold(state)
        kind = clearance.hold_kind if clearance.hold_kind in HOLD_KINDS else "unavailable"
        now = _utcnow().replace(microsecond=0).isoformat()
        since = previous.get("since") if (
            previous.get("status") == "held" and previous.get("kind") == kind) else None
        await state.set_setting(STATUS_KEY, json.dumps({
            "status": "held", "kind": kind, "reason": _clean(clearance.reason)[:300],
            "url": _clean(clearance.fix_url), "waiting": max(1, int(waiting)),
            "since": since or now, "at": now,
        }))
    except Exception as e:
        logger.warning(f"Precleared: failed to record the hold: {e}")


async def clear_hold(state) -> None:
    """Forget a recorded hold once a fresh call has been answered."""
    try:
        if parse_status(await state.get_setting(STATUS_KEY)).get("status") == "held":
            await state.set_setting(STATUS_KEY, "")
    except Exception as e:
        logger.warning(f"Precleared: failed to clear the hold: {e}")


def hold_applies(config) -> bool:
    """Whether the check is on for this config: enabled, on a native provider."""
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS

    settings = getattr(getattr(config, "compliance", None), "precleared", None)
    provider = getattr(getattr(getattr(config, "channels", None), "email", None),
                       "provider", None)
    return getattr(settings, "enabled", False) is True and provider in NATIVE_PROVIDERS


async def current_hold(state, config=None) -> dict:
    """The recorded hold if a cycle hit it recently, else {}.

    Given the loaded config, also {} when the check is off there or the
    provider isn't a native one (the check never runs then). Without one
    (it failed to load) the hold still shows.
    """
    if config is not None and not hold_applies(config):
        return {}
    status = parse_status(await state.get_setting(STATUS_KEY))
    if status.get("status") != "held":
        return {}
    try:
        at = datetime.fromisoformat(str(status.get("at")))
    except (TypeError, ValueError):
        return {}
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if _utcnow() - at > STATUS_MAX_AGE:
        return {}
    return {k: _clean(v) if isinstance(v, str) else v for k, v in status.items()}


def hold_message(hold: dict) -> dict:
    """Title, detail, tone and link for a recorded hold, in plain words.

    Shared by the Today page and `harvey status`, so both say the same thing.
    """
    kind = hold.get("kind")
    url = _text(hold.get("url"))
    if any(unicodedata.category(ch) == "Cc" or ch.isspace() for ch in url):
        url = ""
    try:
        waiting = max(1, int(hold.get("waiting") or 1))
    except (TypeError, ValueError):
        waiting = 1
    emails = f"{waiting} cold email is waiting" if waiting == 1 else f"{waiting} cold emails are waiting"
    replies = "Replies are not held."
    if kind == "keyless_limit":
        return {
            "tone": "warn", "url": url or GET_KEY_URL, "url_label": "Get a free key",
            "title": "Precleared's free checks for today are used up",
            "detail": (f"{emails}. Without a key, Precleared allows 10 checks a day. "
                       f"A free key gives Harvey its own 25 a day: get one at {url or GET_KEY_URL} "
                       "and put it in .env as PRECLEARED_API_KEY. Otherwise the waiting "
                       f"emails go out after the checks reset at midnight UK time. {replies}"),
        }
    if kind == "out_of_checks":
        return {
            "tone": "warn", "url": url or BUY_URL, "url_label": "Buy checks",
            "title": "Your Precleared key is out of checks for today",
            "detail": (f"{emails}. The key has used today's free checks and has no pack "
                       f"left. Buy checks to send them now: {url or BUY_URL}. Otherwise they "
                       "go out after the free checks reset at midnight UK time, or switch the "
                       f"check off (compliance.precleared.enabled: false). {replies}"),
        }
    if kind == "key_rejected":
        return {
            "tone": "bad", "url": url or GET_KEY_URL, "url_label": "Get a new key",
            "title": "Precleared rejected your key",
            "detail": (f"{emails}. PRECLEARED_API_KEY was rejected (wrong or revoked). "
                       "Check the key in .env, get a new free key at "
                       f"{url or GET_KEY_URL}, or switch the check off "
                       f"(compliance.precleared.enabled: false). {replies}"),
        }
    if kind == "rate_limited":
        return {
            "tone": "warn", "url": "", "url_label": "",
            "title": "Precleared is limiting how fast Harvey can check",
            "detail": (f"{emails}. Precleared allows a set number of checks a minute, "
                       f"and Harvey asks again next cycle. {replies}"),
        }
    reason = _text(_clean(_text(hold.get("reason")))) or "no answer"
    return {
        "tone": "warn", "url": "", "url_label": "",
        "title": "Precleared can't be reached right now",
        "detail": (f"{emails} until it answers ({reason}). Harvey asks again every "
                   f"cycle. {replies}"),
    }
