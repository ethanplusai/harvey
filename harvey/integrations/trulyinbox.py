"""TrulyInbox (trulyinbox.com): the warmup service Harvey recommends, and reads.

A new mailbox has no sending history, so it needs a few weeks of warmup before
cold email, and a warmup service running alongside it keeps it healthy after.
Harvey recommends TrulyInbox for that. With a key connected
(TRULYINBOX_API_KEY, `harvey trulyinbox connect` or the dashboard), Harvey
reads whether the sending mailbox is enrolled, whether its warmup is on and
signed in, and how much of its warmup mail landed in spam, so `harvey warmup`
can say whether the mailbox is actually warmed instead of counting days.

Only reads: every request is a GET, and nothing here can enroll, pause,
change or remove anything in the user's TrulyInbox account. API access needs
a paid TrulyInbox plan (Starter or higher) and a workspace key with the
accounts:read, warmup:read and analytics:read scopes (workspace:read lets
Harvey find the workspace on its own; otherwise set TRULYINBOX_WORKSPACE_ID).

TrulyInbox locks an IP out for 15 minutes after five rejected keys, so a key
it rejects is never sent again by this process until it's connected afresh.
The key goes in the Authorization header and nowhere else.

Endpoints follow dev.trulyinbox.com (v2 Public API, read 2026-10-25).
"""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

API_BASE = "https://lynx-api.trulyinbox.com"
# The referral link, used everywhere Harvey points people at TrulyInbox.
AFFILIATE_URL = "https://trulyinbox.com/?via=harvey"
AFFILIATE_TAG = "(affiliate link)"
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
REPORT_DAYS = 14
FIND_LIMIT = 25

# What the user chose on the setup step: connected TrulyInbox, or uses another
# warmup service. Either way Harvey (and the agent) stop asking.
CHOICE_KEY = "trulyinbox:choice"
CHOICES = ("connected", "other")

KEY_REJECTED = ("TrulyInbox rejected the API key. Create a workspace key in TrulyInbox "
                "(Settings > API keys) and connect it again.")
MISSING_SCOPE = ("The TrulyInbox key is missing a scope. Create a workspace key with "
                 "accounts:read, warmup:read, analytics:read and workspace:read.")
NO_API_PLAN = "Your TrulyInbox plan doesn't include API access. API access starts on Starter."
PAUSED = "Your TrulyInbox subscription is paused. Resume it in TrulyInbox."
WRONG_WORKSPACE = "This key belongs to a different TrulyInbox workspace."
NO_WORKSPACE = ("Couldn't find the TrulyInbox workspace. Add the workspace:read scope "
                "to the key, or set TRULYINBOX_WORKSPACE_ID in .env.")
RATE_LIMITED = "TrulyInbox's API limit is reached. Try again in a minute."
LOCKED_OUT = ("TrulyInbox locked this computer out for 15 minutes after rejected keys. "
              "Wait, then try again with a working key.")
BAD_WORKSPACE_ID = "TRULYINBOX_WORKSPACE_ID must be the workspace's number. Fix it in .env."
NO_ADDRESS = ("Harvey doesn't know which mailbox it sends from. Set persona.email in "
              "harvey.yaml first.")
NO_ANSWER = "TrulyInbox didn't answer. Try again in a minute."
UNREADABLE = "TrulyInbox sent a reply Harvey couldn't read."

# Each error rule in both spellings the docs use (6-digit and string codes).
AUTH_CODES = {"150001", "150002", "invalid_api_key", "unauthorized", "Api.InvalidKey"}
# Too many rejected keys from this IP: a 15-minute lock, not a bad key.
LOCKOUT_CODES = {"150006", "Api.TooManyFailedAttempts", "too_many_requests"}
SCOPE_CODES = {"150003", "insufficient_scope", "Api.ScopeForbidden"}
WORKSPACE_CODES = {"150004", "workspace_forbidden", "Api.WorkspaceForbidden"}
NO_API_CODES = {"150007", "api_access_not_in_plan"}
PAUSED_CODES = {"040006", "subscription_paused"}
RATE_CODES = {"150005", "rate_limit_exceeded", "Api.RateLimitExceeded"}

_WARM = {"active", "warming", "enabled", "running"}
_LOGIN_FAILED = ("auth_error", "auth_expired", "auth_failed", "suspended_auth", "blocked",
                 "disconnect")

# Hashes of keys TrulyInbox rejected. Nothing sends them again.
_REJECTED: set[str] = set()


class TrulyInboxError(RuntimeError):
    """Something went wrong talking to TrulyInbox. The message says what to do."""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def forget_rejected(key: str) -> None:
    """Connecting a key clears an earlier rejection of it."""
    _REJECTED.discard(_hash(key))


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _list(x) -> list:
    return x if isinstance(x, list) else []


def _str(x) -> str:
    return x if isinstance(x, str) else ""


def _int(x) -> int:
    if isinstance(x, bool):
        return 0
    if isinstance(x, int):
        return max(0, x)
    if isinstance(x, float) and math.isfinite(x):
        return max(0, int(x))
    return 0


def _code(x) -> str:
    """The error code as a string; a numeric one keeps its leading zeros (040006)."""
    if isinstance(x, bool) or x is None:
        return ""
    code = str(x).strip()
    return code.zfill(6) if code.isdigit() else code


def _day(x) -> date | None:
    if not isinstance(x, str) or not x:
        return None
    try:
        dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).date()


@dataclass(frozen=True)
class MailboxHealth:
    address: str
    found: bool
    warming: bool = False
    login_ok: bool = True
    started_on: date | None = None
    sent: int = 0          # warmup emails sent over the last REPORT_DAYS
    spam: int = 0          # of those, how many landed in spam
    score: float | None = None   # TrulyInbox's own deliverability rate

    @property
    def spam_rate(self) -> float | None:
        return self.spam / self.sent if self.sent else None


class TrulyInbox:
    def __init__(self, api_key: str, workspace_id: str = "", *,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(api_key, str) or not api_key.strip() \
                or not (api_key.isascii() and api_key.isprintable()):
            raise TrulyInboxError(KEY_REJECTED)
        self._key = api_key.strip()
        self.workspace_id = str(workspace_id or "").strip()
        if self.workspace_id and not self.workspace_id.isdigit():
            raise TrulyInboxError(BAD_WORKSPACE_ID)
        self._transport = transport

    def __repr__(self) -> str:
        return f"TrulyInbox(workspace_id={self.workspace_id!r})"

    async def _get(self, path: str, params: dict | None = None):
        """One GET; returns the envelope's data or raises TrulyInboxError."""
        if _hash(self._key) in _REJECTED:
            raise TrulyInboxError(KEY_REJECTED)
        try:
            async with httpx.AsyncClient(base_url=API_BASE, transport=self._transport,
                                         timeout=TIMEOUT) as client:
                resp = await client.get(path, params=params,
                                        headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError:
            raise TrulyInboxError(NO_ANSWER) from None
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = None
        envelope = _dict(body)
        error = _dict(envelope.get("error"))
        code = _code(envelope.get("errorCode")) or _code(error.get("code"))
        if resp.is_success and not code and envelope.get("success") is not False:
            if body is None:
                raise TrulyInboxError(UNREADABLE)
            return envelope.get("data")
        if code in LOCKOUT_CODES:
            raise TrulyInboxError(LOCKED_OUT)
        if resp.status_code == 401 or code in AUTH_CODES:
            _REJECTED.add(_hash(self._key))
            raise TrulyInboxError(KEY_REJECTED)
        for codes, message in ((SCOPE_CODES, MISSING_SCOPE), (NO_API_CODES, NO_API_PLAN),
                               (PAUSED_CODES, PAUSED), (WORKSPACE_CODES, WRONG_WORKSPACE)):
            if code in codes:
                raise TrulyInboxError(message)
        if resp.status_code == 429 or code in RATE_CODES:
            raise TrulyInboxError(RATE_LIMITED)
        if resp.status_code >= 500:
            raise TrulyInboxError(NO_ANSWER)
        message = (_str(envelope.get("errorMessage")) or _str(error.get("message")))
        message = message.replace(self._key, "[hidden]")[:200]
        raise TrulyInboxError(f"TrulyInbox returned {resp.status_code}"
                              + (f": {message}" if message else "."))

    async def _workspace(self) -> str:
        if not self.workspace_id:
            # A number, or it can't go in a URL path or .env.
            spaces = [w for w in _list(await self._get("/v2/workspaces"))
                      if isinstance(w, dict) and str(w.get("id", "")).isdigit()]
            if not spaces:
                raise TrulyInboxError(NO_WORKSPACE)
            self.workspace_id = str(spaces[0]["id"])   # a workspace key sees only its own
        return self.workspace_id

    async def mailbox_health(self, address: str) -> MailboxHealth:
        """What TrulyInbox knows about one mailbox's warmup."""
        address = address.strip()
        base = f"/v2/workspaces/{await self._workspace()}"
        accounts = await self._get(f"{base}/accounts", {"q": address, "limit": FIND_LIMIT})
        hit = next((a for a in _list(accounts) if isinstance(a, dict)
                    and _str(a.get("emailAddress")).strip().lower() == address.lower()
                    and str(a.get("id", "")).isdigit()), None)
        if hit is None:
            return MailboxHealth(address=address, found=False)
        account = f"{base}/accounts/{hit['id']}"
        status = _dict(await self._get(f"{account}/warmup"))
        stats = _dict(await self._get(f"{account}/analytics/stats", {"days": REPORT_DAYS}))
        tokens = [_str(status.get(k)).lower() for k in
                  ("status", "accountState", "statusBucket", "warmupPausedReason")]
        login_ok = not any(w in t for t in tokens for w in _LOGIN_FAILED) \
            and status.get("sendCheckFailed") is not True \
            and status.get("receiveCheckFailed") is not True
        days = [x for x in _list(stats.get("dailyBreakdown")) if isinstance(x, dict)]
        score = stats.get("deliverabilityRate")
        return MailboxHealth(
            address=address, found=True,
            warming=_str(status.get("warmupStatus")).lower() in _WARM and login_ok,
            login_ok=login_ok,
            started_on=_day(status.get("warmupStartDate")),
            sent=sum(_int(x.get("sent")) for x in days),
            spam=sum(_int(x.get("spamDetected")) for x in days),
            score=float(score) if isinstance(score, (int, float))
            and not isinstance(score, bool) and math.isfinite(score) else None,
        )


async def get_choice(state) -> str:
    """'connected', 'other' (uses another warmup service), or '' (not decided)."""
    try:
        choice = _dict(json.loads(await state.get_setting(CHOICE_KEY) or "{}")).get("choice")
    except ValueError:
        return ""
    return choice if choice in CHOICES else ""


async def set_choice(state, choice: str) -> None:
    if choice not in CHOICES:
        raise ValueError(f"choice must be one of {CHOICES}")
    await state.set_setting(CHOICE_KEY, json.dumps(
        {"choice": choice, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))


def sending_address(config) -> str:
    """The mailbox Harvey sends from: the SMTP login when it's an address, else the persona's."""
    import os
    email = config.channels.email
    smtp_user = os.getenv("SMTP_USERNAME", "").strip() if email.provider == "smtp" else ""
    return smtp_user if "@" in smtp_user else config.persona.email


def key_from_env() -> tuple[str, str]:
    import os
    return (os.getenv("TRULYINBOX_API_KEY", "").strip(),
            os.getenv("TRULYINBOX_WORKSPACE_ID", "").strip())


def describe_status(health: MailboxHealth | None, error: str, choice: str) -> str:
    """One line for `harvey trulyinbox`: connected, another service, or not set up."""
    if error:
        return f"connected, but: {error}"
    if health is not None:
        return "connected"
    return {"other": "using another warmup service"}.get(choice, "not set up")


async def current_health(address: str) -> tuple[MailboxHealth | None, str]:
    """(health, "") when connected, (None, error) when TrulyInbox failed, (None, "") when no key."""
    key, workspace = key_from_env()
    if not key:
        return None, ""
    try:
        return await TrulyInbox(key, workspace).mailbox_health(address), ""
    except TrulyInboxError as e:
        return None, str(e)
