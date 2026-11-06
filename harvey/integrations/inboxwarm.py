"""InboxWarm.ai: an optional warmup service Harvey can read instead of TrulyInbox.

A new mailbox needs a few weeks of warmup before cold email, and a warmup
service running alongside it keeps it healthy after. With a key connected
(INBOXWARM_API_KEY, `harvey inboxwarm connect` or the dashboard), Harvey reads
whether the sending mailbox is in InboxWarm, whether it is signed in and
warming, and how much of its warmup mail landed in spam over the last 14 days,
so `harvey warmup` can say whether the mailbox is actually warmed.

Only reads: the requests here list accounts and read status and reports;
nothing connects, starts, stops, edits or deletes anything in InboxWarm (the
deliverability report is a POST only because InboxWarm takes its date range
in a body). API access comes with every InboxWarm plan; the key is created in
the app's Developer Kit (https://app.inboxwarm.ai/developer-kit).

InboxWarm takes the key as an `api_key` query parameter on GET and an
`apiKey` field in POST bodies, so it is never logged here and Harvey's log
keeps httpx quiet (harvey/main.py). The default limit is 20 requests a minute;
one health read is three requests.

Endpoints follow https://inboxwarm.ai/api/docs/ (v1, read 2026-11-03).
"""

import math
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from harvey.integrations.trulyinbox import MailboxHealth, REPORT_DAYS

API_BASE = "https://app.inboxwarm.ai/api/v1"
SIGNUP_URL = "https://inboxwarm.ai/"
KEY_URL = "https://app.inboxwarm.ai/developer-kit"
NAME = "InboxWarm"
OFFER = "Harvey users get 25% off with code HARVEY25."
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

KEY_REJECTED = ("InboxWarm rejected the API key. Copy the key from InboxWarm's "
                "Developer Kit (app.inboxwarm.ai/developer-kit) and connect it again.")
RATE_LIMITED = "InboxWarm's API limit is reached (20 a minute). Try again in a minute."
NO_ANSWER = "InboxWarm didn't answer. Try again in a minute."
UNREADABLE = "InboxWarm sent a reply Harvey couldn't read."
NO_ADDRESS = ("Harvey doesn't know which mailbox it sends from. Set persona.email in "
              "harvey.yaml first.")

_WARM = {"active", "warming", "running", "enabled"}
_SIGNED_IN = {"connected", "active", "ok"}


class InboxWarmError(RuntimeError):
    """A message that is safe to show the user (never contains the key)."""


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _str(x) -> str:
    return x if isinstance(x, str) else ""


def _int(x) -> int:
    if isinstance(x, bool):
        return 0
    if isinstance(x, (int, float)) and math.isfinite(x):
        return max(int(x), 0)
    return 0


def _day(x) -> date | None:
    try:
        dt = datetime.fromisoformat(_str(x).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt.astimezone(timezone.utc) if dt.tzinfo else dt).date()


def key_from_env() -> str:
    import os
    return os.getenv("INBOXWARM_API_KEY", "").strip()


class InboxWarm:
    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 today: date | None = None):
        if not isinstance(api_key, str) or not api_key.strip() \
                or not (api_key.isascii() and api_key.isprintable()):
            raise InboxWarmError(KEY_REJECTED)
        self._key = api_key.strip()
        self._transport = transport
        self._today = today

    def __repr__(self) -> str:
        return "InboxWarm()"

    async def _call(self, method: str, path: str, *, params: dict | None = None,
                    body: dict | None = None):
        """One request; returns the parsed JSON body or raises InboxWarmError."""
        try:
            async with httpx.AsyncClient(base_url=API_BASE, transport=self._transport,
                                         timeout=TIMEOUT) as client:
                if method == "GET":
                    resp = await client.get(path, params={**(params or {}), "api_key": self._key})
                else:
                    resp = await client.post(path, json={**(body or {}), "apiKey": self._key})
        except httpx.HTTPError:
            raise InboxWarmError(NO_ANSWER) from None
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = None
        if resp.is_success:
            if data is None:
                raise InboxWarmError(UNREADABLE)
            return data
        if resp.status_code in (401, 403):
            raise InboxWarmError(KEY_REJECTED)
        if resp.status_code == 429:
            raise InboxWarmError(RATE_LIMITED)
        if resp.status_code >= 500:
            raise InboxWarmError(NO_ANSWER)
        message = _str(_dict(data).get("message")).replace(self._key, "[hidden]")[:200]
        raise InboxWarmError(f"InboxWarm returned {resp.status_code}"
                             + (f": {message}" if message else "."))

    async def mailbox_health(self, address: str) -> MailboxHealth:
        """What InboxWarm knows about one mailbox's warmup."""
        address = address.strip()
        listing = _dict(await self._call("GET", "/email-accounts", params={"search": address}))
        hit = next((a for a in listing.get("items") or [] if isinstance(a, dict)
                    and _str(a.get("fromEmail")).strip().lower() == address.lower()
                    and _str(str(a.get("id", ""))).strip()), None)
        if hit is None:
            return MailboxHealth(address=address, found=False)
        account_id = str(hit["id"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id):   # goes into a URL path
            raise InboxWarmError(UNREADABLE)

        status = _dict(await self._call("GET", f"/email-accounts/{account_id}/status"))
        auth = _str(status.get("authStatus")).lower()
        list_state = _str(hit.get("status")).lower()
        login_ok = (auth in _SIGNED_IN if auth else True) \
            and not status.get("connectionError") \
            and list_state not in ("auth-expired", "error")
        warming = _str(status.get("warmupStatus")).lower() in _WARM and login_ok

        today = self._today or datetime.now(timezone.utc).date()
        report = _dict(await self._call(
            "POST", f"/deliverability-score/{account_id}",
            body={"startDate": (today - timedelta(days=REPORT_DAYS)).isoformat(),
                  "endDate": today.isoformat()}))
        score = report.get("totalDeliverabilityRate")
        return MailboxHealth(
            address=address, found=True, warming=warming, login_ok=login_ok,
            # InboxWarm reports no warmup start date; the day the mailbox was
            # connected to InboxWarm is the closest it gives.
            started_on=_day(hit.get("createdAt")),
            sent=_int(report.get("totalSentCount")),
            spam=_int(report.get("totalSpamInboxCount")),
            score=float(score) if isinstance(score, (int, float))
            and not isinstance(score, bool) and math.isfinite(score) else None,
        )


async def current_health(address: str) -> tuple[MailboxHealth | None, str]:
    """(health, "") when connected, (None, error) when InboxWarm failed, (None, "") when no key."""
    key = key_from_env()
    if not key:
        return None, ""
    try:
        return await InboxWarm(key).mailbox_health(address), ""
    except InboxWarmError as e:
        return None, str(e)
