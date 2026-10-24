"""Optional per-recipient cold-email check through Precleared (precleared.dev).

Off by default. With ``compliance.precleared.enabled: true``, the sender asks
Precleared about each new recipient before the first cold email of a
sequence: may a cold marketing email go to this address under UK PECR, US
CAN-SPAM, Canada's CASL, Germany's UWG or France's rules? Anything other than
``ok_to_email`` holds the email back with the reason, and the signed receipt
id is logged with the send so the decision can be shown later.

One check per address: a definite verdict is cached in the settings table for
CACHE_DAYS, together with a fingerprint of the facts it was decided on (sender
country, address source, product description and, if set, prospect country). A
cached verdict is reused only while those facts are unchanged, so a three-step
sequence costs one check but a config change forces a fresh one. Replies are
never checked. Precleared receives the recipient's address, your sender
country, the address source you configure, your product description (used to
judge relevance under CASL and French rules) and, if you set one, your
prospects' country, which it uses only when it can't work out the recipient's
country from the address, company records or the company's website.

A response Precleared can't vouch for (network trouble, daily allowance used
up, a bad key, or a malformed body) holds cold email for a later cycle instead
of failing it.

Without an API key Precleared allows 10 checks a day; a free key
(https://precleared.dev/get-key, PRECLEARED_API_KEY) allows 25 a day.
Beyond that, a key draws on a prepaid pack. When a key runs out, the held
reason gives the checkout link from Precleared's answer.

A hold is also recorded under STATUS_KEY, so the dashboard's Today page and
`harvey status` say that cold email is waiting and how to fix it (a free
key, a pack, or nothing but time). The next fresh answer clears it.
"""

import hashlib
import ipaddress
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import httpx

from harvey.config import normalize_prospect_country

logger = logging.getLogger("harvey.precleared")

API_URL = "https://precleared.dev/api/v1/check"
# Precleared answers within 25 seconds (with verdict "unknown" if a register
# lookup is slow), so the read timeout sits just above that.
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
CACHE_DAYS = 30
# Bump when the facts in _fingerprint change, so older cache entries are
# ignored rather than reused.
CACHE_VERSION = 2
SETTINGS_PREFIX = "precleared:"
VERDICTS = {"ok_to_email", "consent_needed", "blocked", "unknown"}
DEFINITE_VERDICTS = {"ok_to_email", "consent_needed", "blocked"}
ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")
SENDER_COUNTRIES = ("GB", "US")
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
HOLD_KINDS = ("keyless_limit", "out_of_checks", "key_rejected", "unavailable")


@dataclass
class Clearance:
    """Precleared's answer for one address.

    ``allowed``: send it. ``held``: the check could not be made right now
    (network trouble, daily allowance used up, bad key, malformed response),
    so the email stays queued and is tried again on a later cycle. Otherwise
    the email is blocked and ``reason`` says why.
    """

    allowed: bool
    held: bool = False
    verdict: str = ""
    reason: str = ""
    receipt_id: str = ""
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
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.sender_country = sender_country
        self.api_key = api_key or ""
        self.address_source = address_source
        self.pitch = (pitch or "").strip()[:200]
        self.prospect_country = normalize_prospect_country(prospect_country)
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
        )

    def _redact(self, text: str) -> str:
        if self.api_key and text:
            return text.replace(self.api_key, "***REDACTED***")
        return text or ""

    def _fingerprint(self) -> str:
        """Hash of the facts Precleared decides on, apart from the address.

        These are exactly the fields _check sends with the address, so a
        cached verdict is only reused for the same question.
        """
        facts = {
            "v": CACHE_VERSION,
            "sender_country": self.sender_country,
            "address_source": self.address_source,
            "pitch": self.pitch,
            "existing_customer": False,
        }
        if self.prospect_country:
            # Only when set, so cached verdicts from before the setting existed
            # still match.
            facts["prospect_country"] = self.prospect_country
        return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()

    async def clear(self, to_email: str, state) -> Clearance:
        """Return the verdict for one address, from the cache when possible."""
        email = (to_email or "").strip().lower()
        key = SETTINGS_PREFIX + email
        inputs = self._fingerprint()
        cached = _from_cache(await state.get_setting(key), inputs)
        if cached is not None:
            return cached

        result = await self._check(email)
        if not result.held and result.verdict in DEFINITE_VERDICTS:
            # One entry per address: a fresh verdict replaces any earlier
            # one, including legacy entries written without a fingerprint.
            await state.set_setting(key, json.dumps({
                "verdict": result.verdict,
                "reason": result.reason,
                "receipt_id": result.receipt_id,
                "checked_at": _utcnow().isoformat(),
                "inputs": inputs,
            }))
        return result

    async def _check(self, email: str) -> Clearance:
        payload = {
            "recipient_email": email,
            "sender_country": self.sender_country,
            "address_source": self.address_source,
            "existing_customer": False,
        }
        if self.pitch:
            payload["pitch"] = self.pitch
        if self.prospect_country:
            # Precleared uses this only when the address, company records and
            # the company's website don't show the recipient's country.
            payload["default_recipient_country"] = self.prospect_country
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "harvey (+https://github.com/ethanplusai/harvey)",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                resp = await client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            return Clearance(allowed=False, held=True, hold_kind="unavailable",
                             reason=self._redact(f"Precleared unreachable: {e}"))

        if resp.status_code == 429:
            # Keyless allowance used up: a free key gives this install its own.
            return Clearance(
                allowed=False, held=True, hold_kind="keyless_limit", fix_url=GET_KEY_URL,
                reason="Precleared daily allowance used up; add PRECLEARED_API_KEY "
                       "(free key: https://precleared.dev/get-key) or wait until tomorrow",
            )
        if resp.status_code == 402:
            # The key's free checks for today are used and it has no credits left,
            # so another key won't help: a pack (or tomorrow's allowance) will.
            # The 402 answer carries a checkout link for this key.
            buy_url = _buy_url(resp, self.api_key)
            return Clearance(
                allowed=False, held=True, hold_kind="out_of_checks", fix_url=buy_url,
                reason=self._redact(
                    "Precleared: today's free checks on your key are used and it has "
                    f"no credits left; add a pack ({buy_url}) "
                    "or wait until tomorrow"
                ),
            )
        if resp.status_code == 401:
            return Clearance(allowed=False, held=True, hold_kind="key_rejected",
                             fix_url=GET_KEY_URL,
                             reason="PRECLEARED_API_KEY was rejected (wrong or revoked)")
        if resp.status_code != 200:
            return Clearance(allowed=False, held=True, hold_kind="unavailable",
                             reason=self._redact(f"Precleared HTTP {resp.status_code}: {resp.text[:200]}"))

        try:
            data = resp.json()
        except ValueError:
            return Clearance(allowed=False, held=True, hold_kind="unavailable",
                             reason="Precleared returned invalid JSON")

        # Only a JSON object with a known verdict counts as an answer.
        # Anything else is treated like an unavailable check, so cold email
        # is held, not failed, and the rest of the cycle carries on.
        if not isinstance(data, dict):
            return Clearance(allowed=False, held=True, hold_kind="unavailable",
                             reason="Precleared returned an unexpected response (not a JSON object)")
        verdict = data.get("verdict")
        if not isinstance(verdict, str) or verdict not in VERDICTS:
            return Clearance(allowed=False, held=True, hold_kind="unavailable",
                             reason="Precleared returned a response without a known verdict")

        receipt = data.get("receipt")
        receipt_id = _text(receipt.get("id")) if isinstance(receipt, dict) else ""
        if verdict == "ok_to_email":
            return Clearance(allowed=True, verdict=verdict, receipt_id=receipt_id)
        return Clearance(
            allowed=False,
            verdict=verdict,
            reason=_reason(data),
            receipt_id=receipt_id,
        )


def _text(value) -> str:
    """A stripped string, or "" for anything that isn't one."""
    return value.strip() if isinstance(value, str) else ""


def _buy_url(resp: httpx.Response, api_key: str = "") -> str:
    """The checkout link from a 402 answer's ``buy_url``, or BUY_URL.

    Only a plain https link of sane length is used; a missing, malformed or
    non-JSON body falls back to Precleared's buy page.
    """
    try:
        data = resp.json()
    except ValueError:
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
    except (ValueError, UnicodeError):
        return BUY_URL

    if api_key:
        # Inspect decoded copies, but keep a usable checkout/session URL intact.
        # Reject excessive nesting instead of logging a credential we could not
        # finish checking. unquote handles upper/lower-case percent escapes.
        decoded = url
        for _ in range(8):
            if api_key in decoded:
                return BUY_URL
            next_decoded = unquote(decoded)
            if next_decoded == decoded:
                return url
            decoded = next_decoded
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
        previous = parse_status(await state.get_setting(STATUS_KEY))
        kind = clearance.hold_kind if clearance.hold_kind in HOLD_KINDS else "unavailable"
        now = _utcnow().replace(microsecond=0).isoformat()
        since = previous.get("since") if (
            previous.get("status") == "held" and previous.get("kind") == kind) else None
        await state.set_setting(STATUS_KEY, json.dumps({
            "status": "held", "kind": kind, "reason": clearance.reason[:300],
            "url": clearance.fix_url, "waiting": max(1, int(waiting)),
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


async def current_hold(state) -> dict:
    """The recorded hold if a cycle hit it recently, else {}."""
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
    return status


def hold_message(hold: dict) -> dict:
    """Title, detail, tone and link for a recorded hold, in plain words.

    Shared by the Today page and `harvey status`, so both say the same thing.
    """
    kind = hold.get("kind")
    url = _text(hold.get("url"))
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
                       f"go out after the free checks reset at midnight UK time. {replies}"),
        }
    if kind == "key_rejected":
        return {
            "tone": "bad", "url": url or GET_KEY_URL, "url_label": "Get a new key",
            "title": "Precleared rejected your key",
            "detail": (f"{emails}. PRECLEARED_API_KEY was rejected (wrong or revoked). "
                       "Check the key in .env, or get a new free key at "
                       f"{url or GET_KEY_URL}. {replies}"),
        }
    reason = _text(hold.get("reason")) or "no answer"
    return {
        "tone": "warn", "url": "", "url_label": "",
        "title": "Precleared can't be reached right now",
        "detail": (f"{emails} until it answers ({reason}). Harvey asks again every "
                   f"cycle. {replies}"),
    }
