"""Several sending inboxes (seam S6).

The main mailbox is the one `channels.email.provider` configures, exactly as
before. Besides it, the user can connect inboxes they already own: a Gmail or
Microsoft 365 sign-in, or an SMTP/IMAP login. Each extra inbox is a row in
`mailboxes`; its sign-in or password lives in the system keychain
(sellbird.secrets), never in SQLite or a file.

The main mailbox is never a row. Its id is MAIN (""), so every outbox row and
conversation written before this existed is already the main mailbox's, and
an install with no extra inbox sends exactly as it did.

One drain cycle works on an `Inboxes`, read once:

- a new thread (a prospect's first email in a campaign) goes to the inbox
  with the most room left today, among those the campaign may use;
- once anything in a thread has gone out, its inbox never changes, so every
  follow-up and reply leaves from the address the prospect knows;
- each extra inbox has its own daily cold cap (`per_inbox_daily_cap`, or the
  user's own number for that inbox, under the warmup ramp from the day it
  started warming); the main mailbox keeps `max_daily_sends` and its ramp;
- a paused inbox sends nothing, and its unsent first emails move to another.

What a connection is (OAuth, SMTP) is an engine concern; an extension can add
another kind with ``registry.add_mailbox_connection`` (sellbird.extensions).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from typing import Literal

import aiosqlite

from sellbird.config import WarmupConfig, cold_daily_cap
from sellbird.integrations.mail_provider import MailIdentity, MailProvider, identity_from_config
from sellbird.secrets import SecretStore, SecretStoreUnavailable
from sellbird.state import StateManager, _new_id, _norm

logger = logging.getLogger("sellbird.mailboxes")

MAIN = ""                       # the main mailbox's id
PROVIDERS = ("google", "microsoft", "smtp")
BUILT_IN_CONNECTIONS = ("oauth", "smtp")
STATES = ("active", "retired")
# A user's own number never goes past this per inbox: well below Google's and
# Microsoft's daily sending limits.
HARD_MAX_INBOX_CAP = 500
PER_INBOX_CAP_DEFAULT = 30

# The sequence rows of a thread that haven't left yet, and the ones that have
# (or may have: 'sending' is mid-send).
_UNSENT = ("pending_review", "approved")
_STARTED = ("sent", "sending")

# Windows Credential Manager holds about 1280 characters. Only what a refresh
# needs is stored; anything still too big is refused on every OS alike.
MAX_KEYCHAIN_TOKEN_CHARS = 1200
_KEPT_TOKEN_FIELDS = ("refresh_token", "tenant")


class MailboxExists(Exception):
    """That address is already connected."""


class ConnectRefused(Exception):
    """Why an inbox can't be connected. The message is shown as-is."""


class TokenTooLarge(ConnectRefused):
    def __init__(self, provider: str = "Microsoft"):
        super().__init__(
            f"This {provider} sign-in is too large for the Windows keychain, so it can't "
            "be stored. Connect this inbox with its SMTP login instead.")


# ── Keychain refs and tokens ──

def oauth_ref(mailbox_id: str) -> str:
    return f"mailbox:{mailbox_id}:oauth"


def smtp_ref(mailbox_id: str) -> str:
    return f"mailbox:{mailbox_id}:smtp"


def keychain_token(token: dict, provider: str = "Microsoft") -> str:
    """The token as the keychain keeps it: the refresh token (and tenant)
    only, never the access token, scopes or expiry."""
    kept = {k: token[k] for k in _KEPT_TOKEN_FIELDS if token.get(k)}
    raw = json.dumps(kept, separators=(",", ":"))
    if len(raw) > MAX_KEYCHAIN_TOKEN_CHARS:
        raise TokenTooLarge(provider)
    return raw


class KeychainTokenSource:
    """An inbox's OAuth token, kept as JSON in the keychain (a TokenSource)."""

    def __init__(self, secrets: SecretStore, ref: str):
        self._secrets = secrets
        self._ref = ref

    def load(self) -> dict | None:
        raw = self._secrets.get(self._ref)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning(f"Unreadable sign-in in the keychain for {self._ref}")
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def save(self, data: dict) -> None:
        # Called mid-send when Microsoft rotates the refresh token. A locked
        # keychain must not turn that send into a crash: the access token in
        # hand still works, and the next read retries.
        try:
            self._secrets.set(self._ref, keychain_token(data))
        except TokenTooLarge as e:
            logger.warning(f"Kept the old sign-in for {self._ref}: {e}")
        except SecretStoreUnavailable:
            logger.warning(f"Couldn't store the refreshed sign-in for {self._ref}: "
                           "the keychain is unavailable")

    def missing_reason(self) -> str:
        if not self._secrets.available():
            return ("The keychain is locked or unavailable, so this inbox's sign-in can't be "
                    "read. Unlock it, or connect the inbox again.")
        return "This inbox isn't connected. Connect it again: sellbird inboxes connect"

    def __repr__(self) -> str:
        return f"KeychainTokenSource({self._ref!r})"


# ── Storage ──

class MailboxStore(StateManager):
    """The `mailboxes` and `campaign_mailboxes` tables, and the outbox
    queries routing needs. Every table and column name here is fixed."""

    async def _rows(self, query: str, args: tuple = ()) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _one(self, query: str, args: tuple) -> dict | None:
        rows = await self._rows(query, args)
        return rows[0] if rows else None

    async def list_mailboxes(self, *, include_retired: bool = False) -> list[dict]:
        where = "" if include_retired else " WHERE state != 'retired'"
        return await self._rows(f"SELECT * FROM mailboxes{where} ORDER BY created_at, address")

    async def get_mailbox(self, mailbox_id: str) -> dict | None:
        return await self._one("SELECT * FROM mailboxes WHERE id = ?", (mailbox_id,))

    async def get_mailbox_by_address(self, address: str) -> dict | None:
        return await self._one("SELECT * FROM mailboxes WHERE address = ?", (_norm(address),))

    async def add_mailbox(self, *, mailbox_id: str, address: str, provider: str,
                          connection: str, display_name: str = "", credential_ref: str = "",
                          settings: dict | None = None, warmup_started_at: str = "") -> str:
        addr = _norm(address)
        if "@" not in addr:
            raise ValueError("address must be an email address")
        if provider not in PROVIDERS and connection in BUILT_IN_CONNECTIONS:
            raise ValueError(f"unknown mailbox provider {provider!r}")
        async with self._connect() as db:
            # An inbox removed before and connected again: the same row,
            # active again with its new connection (its id names it everywhere).
            cur = await db.execute(
                "UPDATE mailboxes SET state = 'active', provider = ?, connection = ?,"
                " display_name = ?, credential_ref = ?, settings_json = ?,"
                " warmup_started_at = ?, paused_reason = '', cap_override = NULL"
                " WHERE id = ? AND address = ? AND state = 'retired'",
                (provider, connection, (display_name or "").strip(), credential_ref,
                 json.dumps(settings or {}), warmup_started_at or None, mailbox_id, addr))
            if cur.rowcount:
                await db.commit()
                return mailbox_id
            try:
                # The UNIQUE address is the duplicate check, so two connects
                # of one address at once can't both get in.
                await db.execute(
                    "INSERT INTO mailboxes (id, address, display_name, provider, connection,"
                    " credential_ref, settings_json, warmup_started_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (mailbox_id, addr, (display_name or "").strip(), provider, connection,
                     credential_ref, json.dumps(settings or {}), warmup_started_at or None))
            except sqlite3.IntegrityError as e:
                if "mailboxes.address" in str(e):
                    raise MailboxExists(addr) from None
                raise
            await db.commit()
        return mailbox_id

    async def set_pause(self, mailbox_id: str, reason: str) -> bool:
        """Pause an inbox with a reason; an empty reason resumes it."""
        async with self._connect() as db:
            cur = await db.execute("UPDATE mailboxes SET paused_reason = ? WHERE id = ?",
                                   ((reason or "").strip(), mailbox_id))
            await db.commit()
            return cur.rowcount > 0

    async def set_cap(self, mailbox_id: str, cap: int | None) -> bool:
        """The user's own daily cold cap for one inbox; None goes back to the default."""
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int)
                                or not 1 <= cap <= HARD_MAX_INBOX_CAP):
            raise ValueError(f"cap must be between 1 and {HARD_MAX_INBOX_CAP}")
        async with self._connect() as db:
            cur = await db.execute("UPDATE mailboxes SET cap_override = ? WHERE id = ?",
                                   (cap, mailbox_id))
            await db.commit()
            return cur.rowcount > 0

    async def set_display_name(self, mailbox_id: str, name: str) -> bool:
        async with self._connect() as db:
            cur = await db.execute("UPDATE mailboxes SET display_name = ? WHERE id = ?",
                                   ((name or "").strip(), mailbox_id))
            await db.commit()
            return cur.rowcount > 0

    async def retire(self, mailbox_id: str) -> bool:
        """Stop using an inbox for good. Its unsent first emails move to
        another inbox at the next send; threads already started there end."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE mailboxes SET state = 'retired' WHERE id = ? AND state != 'retired'",
                (mailbox_id,))
            await db.execute("DELETE FROM campaign_mailboxes WHERE mailbox_id = ?", (mailbox_id,))
            await db.commit()
            return cur.rowcount > 0

    async def set_campaign_pool(self, campaign_id: str, mailbox_ids: list[str]) -> None:
        """Replace which inboxes a campaign sends from (empty = every inbox)."""
        async with self._connect() as db:
            await db.execute("DELETE FROM campaign_mailboxes WHERE campaign_id = ?", (campaign_id,))
            await db.executemany(
                "INSERT INTO campaign_mailboxes (campaign_id, mailbox_id) VALUES (?, ?)",
                [(campaign_id, m) for m in dict.fromkeys(mailbox_ids)])
            await db.commit()

    async def campaign_pool(self, campaign_id: str) -> set[str]:
        rows = await self._rows(
            "SELECT mailbox_id FROM campaign_mailboxes WHERE campaign_id = ?", (campaign_id,))
        return {r["mailbox_id"] or MAIN for r in rows}

    async def campaign_pools(self) -> dict[str, set[str]]:
        pools: dict[str, set[str]] = {}
        for r in await self._rows("SELECT campaign_id, mailbox_id FROM campaign_mailboxes"):
            pools.setdefault(r["campaign_id"], set()).add(r["mailbox_id"] or MAIN)
        return pools

    # ── What routing reads from the outbox ──

    async def sent_since(self, mailbox_id: str, since_iso: str, kind: str | None = None) -> int:
        """Emails this inbox sent since a naive UTC time."""
        query = ("SELECT COUNT(*) FROM outbox WHERE COALESCE(mailbox_id, '') = ?"
                 " AND status = 'sent' AND sent_at >= ?")
        args: tuple = (mailbox_id, since_iso)
        if kind:
            query += " AND kind = ?"
            args += (kind,)
        async with self._connect() as db:
            async with db.execute(query, args) as cur:
                return (await cur.fetchone())[0]

    async def queued_first_emails(self) -> dict[str, int]:
        """Unsent first emails waiting on each inbox: room they will take."""
        rows = await self._rows(
            "SELECT COALESCE(mailbox_id, '') AS m, COUNT(*) AS n FROM outbox"
            " WHERE kind = 'sequence' AND step <= 1 AND status IN ('pending_review', 'approved')"
            " GROUP BY COALESCE(mailbox_id, '')")
        return {r["m"]: r["n"] for r in rows}

    async def unsent_off_main(self) -> bool:
        """Whether any email still to go is on an inbox other than the main one."""
        row = await self._one(
            "SELECT 1 FROM outbox WHERE COALESCE(mailbox_id, '') != ''"
            " AND status IN ('pending_review', 'approved') LIMIT 1", ())
        return row is not None

    async def thread_started(self, campaign_id: str, prospect_id: str) -> bool:
        """Whether any email of this thread went out (or is going out). From
        then on the thread's inbox never changes."""
        marks = ", ".join("?" * len(_STARTED))
        row = await self._one(
            "SELECT 1 FROM outbox WHERE COALESCE(campaign_id, '') = ? AND prospect_id = ?"
            f" AND kind = 'sequence' AND status IN ({marks}) LIMIT 1",
            (campaign_id or "", prospect_id, *_STARTED))
        return row is not None

    async def thread_mailbox(self, campaign_id: str, prospect_id: str) -> str:
        """The inbox a thread's first email is on ('' when none or the main one)."""
        row = await self._one(
            "SELECT COALESCE(mailbox_id, '') AS m FROM outbox WHERE COALESCE(campaign_id, '') = ?"
            " AND prospect_id = ? AND kind = 'sequence' ORDER BY step LIMIT 1",
            (campaign_id or "", prospect_id))
        return row["m"] if row else MAIN

    async def set_thread_mailbox(self, campaign_id: str, prospect_id: str, mailbox_id: str) -> None:
        """Put every unsent email of a thread on one inbox."""
        marks = ", ".join("?" * len(_UNSENT))
        async with self._connect() as db:
            await db.execute(
                "UPDATE outbox SET mailbox_id = ? WHERE COALESCE(campaign_id, '') = ?"
                f" AND prospect_id = ? AND kind = 'sequence' AND status IN ({marks})",
                (mailbox_id, campaign_id or "", prospect_id, *_UNSENT))
            await db.commit()

    async def conversation_mailbox(self, conversation_id: str) -> str:
        row = await self._one("SELECT COALESCE(mailbox_id, '') AS m FROM conversations"
                              " WHERE id = ?", (conversation_id,))
        return row["m"] if row else MAIN


# ── Caps ──

def _local_midnight_utc_iso(now: datetime) -> str:
    """Local midnight today, as the naive UTC text sent_at is stored in."""
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def per_inbox_cap(email_cfg) -> int:
    value = getattr(email_cfg, "per_inbox_daily_cap", PER_INBOX_CAP_DEFAULT)
    return value if isinstance(value, int) and value > 0 else PER_INBOX_CAP_DEFAULT


def inbox_cold_cap(row: dict, email_cfg, today: date | None = None) -> int:
    """Today's cold-email cap for one extra inbox: the user's own number, or
    the per-inbox default under the warmup ramp from the day it started
    warming (the shape of `channels.email.warmup`, this inbox's date)."""
    if row.get("cap_override") is not None:
        return int(row["cap_override"])
    shape = getattr(email_cfg, "warmup", None)
    if not isinstance(shape, WarmupConfig):
        shape = WarmupConfig()
    ramp = WarmupConfig(start_date=str(row.get("warmup_started_at") or "")[:10],
                        min_days=shape.min_days, start_sends=shape.start_sends,
                        daily_increase=shape.daily_increase)
    return ramp.daily_cap(per_inbox_cap(email_cfg), today)


def _ramped(email_cfg, ceiling: int, today: date) -> int:
    """`ceiling` under the main mailbox's warmup ramp (channels.email.warmup)."""
    warmup = getattr(email_cfg, "warmup", None)
    return warmup.daily_cap(ceiling, today) if isinstance(warmup, WarmupConfig) else ceiling


# ── Transports ──

def _smtp_settings(row: dict, password: str):
    from sellbird.integrations.smtp_mail import SmtpSettings

    try:
        stored = json.loads(row.get("settings_json") or "{}")
    except ValueError:
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    known = {f.name for f in fields(SmtpSettings)} - {"password"}
    kept = {k: v for k, v in stored.items() if k in known}
    kept.setdefault("host", "")
    kept.setdefault("username", row.get("address", ""))
    return SmtpSettings(**kept, password=password)


def build_provider(row: dict, *, config, env, secrets: SecretStore) -> MailProvider | None:
    """The MailProvider one extra inbox sends and reads with, or None when it
    has none (retired, or a connection nothing knows how to build)."""
    if row.get("state") == "retired":
        return None
    address = row.get("address", "") or ""
    identity = MailIdentity(address, (row.get("display_name") or "").strip()
                            or identity_from_config(config).display_name)
    connection, provider = row.get("connection", ""), row.get("provider", "")
    if connection == "oauth":
        tokens = KeychainTokenSource(secrets, oauth_ref(row["id"]))
        if provider == "google":
            from sellbird.integrations.gmail import GmailProvider
            return GmailProvider(config, env, identity=identity, tokens=tokens)
        if provider == "microsoft":
            from sellbird.integrations.microsoft import MicrosoftProvider
            return MicrosoftProvider(config, env, identity=identity, tokens=tokens)
        return None
    if connection == "smtp":
        from sellbird.integrations.smtp_mail import SmtpImapProvider
        settings = _smtp_settings(row, secrets.get(smtp_ref(row["id"])) or "")
        return SmtpImapProvider(config, env, identity=identity, smtp=settings)
    from sellbird import extensions
    builder = extensions.registry().mailbox_connections.get(connection)
    if builder is None:
        return None
    return builder(row, config=config, env=env, secrets=secrets)


# ── Connecting an inbox ──

def check_provider_allows_inboxes(config) -> None:
    from sellbird.integrations.mail_provider import NATIVE_PROVIDERS

    provider = (getattr(config.channels.email, "provider", "") or "").strip().lower()
    if provider in NATIVE_PROVIDERS:
        return
    name = "Instantly" if provider == "instantly" else (provider or "not set")
    raise ConnectRefused(
        f"Your sending provider is {name}. Instantly installs send through Instantly "
        "only. Set channels.email.provider to gmail, microsoft or smtp before "
        "connecting inboxes.")


async def _check_address(store: MailboxStore, config, address: str) -> tuple[str, str]:
    """The normalised address, and the id it will have: a new one, or the
    id it had when it was removed before."""
    addr = _norm(address)
    if "@" not in addr:
        raise ConnectRefused("Enter the inbox's full email address.")
    if addr == _norm(getattr(config.persona, "email", "") or ""):
        raise ConnectRefused("That's already your main mailbox.")
    existing = await store.get_mailbox_by_address(addr)
    if existing and existing.get("state") != "retired":
        raise ConnectRefused("That inbox is already connected.")
    return addr, existing["id"] if existing else _new_id()


def check_warmup_start(value: str) -> str:
    """Blank (an established inbox) or a YYYY-MM-DD no later than tomorrow."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        day = date.fromisoformat(value[:10])
    except ValueError:
        raise ConnectRefused("Give the day this inbox started warming up as YYYY-MM-DD, "
                             "or leave it out for an established inbox.") from None
    if (day - datetime.now(timezone.utc).date()).days > 1:
        raise ConnectRefused("The warmup start can't be in the future.")
    return day.isoformat()


async def _insert(store: MailboxStore, **row) -> str:
    try:
        return await store.add_mailbox(**row)
    except MailboxExists:
        raise ConnectRefused("That inbox is already connected.") from None


async def connect_oauth_mailbox(store: MailboxStore, secrets: SecretStore, config, *,
                                provider: str, address: str, display_name: str,
                                warmup_started_at: str, token: dict) -> str:
    """Record a Gmail or Microsoft inbox after its browser sign-in. The
    keychain is written first, so no row is ever left without its sign-in."""
    check_provider_allows_inboxes(config)
    addr, mailbox_id = await _check_address(store, config, address)
    warmup = check_warmup_start(warmup_started_at)
    ref = oauth_ref(mailbox_id)
    secrets.set(ref, keychain_token(token, "Google" if provider == "google" else "Microsoft"))
    return await _insert(store, mailbox_id=mailbox_id, address=addr, provider=provider,
                         connection="oauth", display_name=display_name,
                         credential_ref=ref, warmup_started_at=warmup)


async def connect_smtp_mailbox(store: MailboxStore, secrets: SecretStore, config, *,
                               address: str, display_name: str, warmup_started_at: str,
                               settings) -> str:
    check_provider_allows_inboxes(config)
    addr, mailbox_id = await _check_address(store, config, address)
    warmup = check_warmup_start(warmup_started_at)
    if not settings.host:
        raise ConnectRefused("Give the SMTP server's host name.")
    ref = smtp_ref(mailbox_id)
    secrets.set(ref, settings.password)
    return await _insert(store, mailbox_id=mailbox_id, address=addr, provider="smtp",
                         connection="smtp", display_name=display_name, credential_ref=ref,
                         settings=settings.public(), warmup_started_at=warmup)


# ── One cycle's view of every inbox ──

@dataclass
class Inbox:
    id: str
    address: str
    name: str
    provider: MailProvider | None
    cap: int                      # today's cold cap
    sent_today: int               # cold emails sent today
    queued_first: int             # unsent first emails waiting here
    paused_reason: str = ""
    sent_this_cycle: int = 0

    @property
    def main(self) -> bool:
        return self.id == MAIN

    @property
    def connected(self) -> bool:
        return self.provider is not None and self.provider.is_configured()

    @property
    def cold_left(self) -> int:
        return max(0, self.cap - self.sent_today)

    @property
    def room(self) -> int:
        """Room after the first emails already waiting here (can go negative)."""
        return self.cap - self.sent_today - self.queued_first


@dataclass(frozen=True)
class Route:
    action: Literal["send", "hold", "cancel"]
    mailbox_id: str = MAIN
    reason: str = ""


@dataclass
class Inboxes:
    """Every inbox one cycle may send from, read once (S6)."""

    store: MailboxStore
    inboxes: dict[str, Inbox]
    pools: dict[str, set[str]] = field(default_factory=dict)
    # Unsent email is on an inbox other than the main one (say, one since
    # removed): it must still be routed, never sent from the main mailbox.
    routed_mail: bool = False

    @property
    def several(self) -> bool:
        """Whether sending routes by inbox: an extra inbox is connected, or
        unsent email is on one."""
        return self.routed_mail or any(not i.main for i in self.inboxes.values())

    def get(self, mailbox_id: str) -> Inbox | None:
        return self.inboxes.get(mailbox_id or MAIN)

    def sendable(self) -> list[Inbox]:
        return [i for i in self.inboxes.values() if i.connected and not i.paused_reason]

    def readers(self) -> list[Inbox]:
        """Every inbox whose replies must be read before anything is sent."""
        return [i for i in self.inboxes.values() if i.connected]

    def eligible(self, campaign_id: str) -> list[Inbox]:
        """Inboxes a new thread of this campaign may start on: sendable, and in
        the campaign's pool when it has one."""
        pool = self.pools.get(campaign_id or "")
        return [i for i in self.sendable() if not pool or i.id in pool]

    def pick(self, campaign_id: str) -> Inbox | None:
        """Most room left, then the main mailbox, then the lowest address."""
        candidates = self.eligible(campaign_id)
        if not candidates:
            return None
        return min(candidates, key=lambda i: (-i.room, not i.main, i.address))

    async def assign(self, campaign_id: str, prospect_id: str, *,
                     staged_now: bool = False) -> str | None:
        """Give a thread an inbox and put its unsent emails on it.

        staged_now: its emails were staged after this cycle was read, so no
        inbox counted its first email yet. Otherwise it moves, and the inbox
        it leaves gets that room back."""
        inbox = self.pick(campaign_id)
        if inbox is None:
            return None
        before = None if staged_now else await self.store.thread_mailbox(campaign_id, prospect_id)
        await self.store.set_thread_mailbox(campaign_id, prospect_id, inbox.id)
        if before != inbox.id:
            old = self.get(before) if before is not None else None
            if old is not None:
                old.queued_first -= 1
            inbox.queued_first += 1
        return inbox.id

    async def route(self, item: dict) -> Route:
        """Which inbox this email leaves from, or why it waits or never will.

        An unsent first email moves only when its inbox can't be used, never
        for room. Once anything in a thread has gone out its inbox is fixed.
        """
        mailbox_id = item.get("mailbox_id") or MAIN
        campaign_id = item.get("campaign_id") or ""
        if item.get("kind") == "sequence":
            if int(item.get("step") or 1) <= 1 and not await self.store.thread_started(
                    campaign_id, item["prospect_id"]):
                if mailbox_id not in {i.id for i in self.eligible(campaign_id)}:
                    moved = await self.assign(campaign_id, item["prospect_id"])
                    if moved is None:
                        return Route("hold", mailbox_id, "no inbox is available for this campaign")
                    item["mailbox_id"] = moved
                    return Route("send", moved)
                return Route("send", mailbox_id)
            if mailbox_id == MAIN:
                # Staged before its thread had an inbox: follow the first email.
                mailbox_id = await self.store.thread_mailbox(campaign_id, item["prospect_id"])
        elif mailbox_id == MAIN and item.get("conversation_id"):
            mailbox_id = await self.store.conversation_mailbox(item["conversation_id"])
        item["mailbox_id"] = mailbox_id
        inbox = self.get(mailbox_id)
        if inbox is None:
            return Route("cancel", mailbox_id, "its inbox was removed")
        if not inbox.connected:
            return Route("hold", mailbox_id, f"{inbox.address or 'the main mailbox'} isn't connected")
        if inbox.paused_reason:
            return Route("hold", mailbox_id, f"its inbox is paused: {inbox.paused_reason}")
        return Route("send", mailbox_id)


def _fingerprint(row: dict) -> tuple:
    return tuple(row.get(k) for k in ("connection", "provider", "address", "display_name",
                                      "credential_ref", "settings_json"))


async def load_inboxes(state, config, env, main_provider: MailProvider | None, *,
                       secrets: SecretStore | None = None,
                       now: datetime | None = None,
                       cache: dict | None = None) -> Inboxes:
    """Every inbox for this cycle: the main mailbox (when the install has a
    native provider) and each connected extra inbox.

    `cache` (a dict the caller keeps) reuses each inbox's provider across
    cycles while its row is unchanged, so a Gmail inbox keeps its access
    token and where its last read ended, as the main mailbox does."""
    store = state if isinstance(state, MailboxStore) else MailboxStore(db_path=state.db_path)
    secrets = secrets if secrets is not None else SecretStore()
    now = now or datetime.now().astimezone()
    email_cfg = config.channels.email
    since = _local_midnight_utc_iso(now)
    today = now.astimezone().date()
    queued = await store.queued_first_emails()
    inboxes: dict[str, Inbox] = {}
    rows = await store.list_mailboxes()
    routed = await store.unsent_off_main()
    if main_provider is not None:
        persona = identity_from_config(config)
        if rows or routed:
            # One inbox among several: the per-inbox cap under the main
            # mailbox's own ramp; max_daily_sends is then the day's total.
            cap = cold_daily_cap(email_cfg, today)
            cap = min(cap, _ramped(email_cfg, per_inbox_cap(email_cfg), today))
        else:
            cap = cold_daily_cap(email_cfg, today)   # exactly as before
        inboxes[MAIN] = Inbox(
            id=MAIN, address=persona.address, name=persona.display_name,
            provider=main_provider, cap=cap,
            sent_today=await store.sent_since(MAIN, since, kind="sequence"),
            queued_first=queued.get(MAIN, 0))
    if cache is not None:
        for gone in set(cache) - {r["id"] for r in rows}:
            del cache[gone]
    for row in rows:
        kept = cache.get(row["id"]) if cache is not None else None
        if kept is not None and kept[0] == _fingerprint(row):
            provider = kept[1]
        else:
            try:
                provider = build_provider(row, config=config, env=env, secrets=secrets)
            except Exception as e:
                # One bad row must not take every inbox down. Type name only:
                # the message could carry settings.
                logger.error(f"Inbox {row.get('address', '')}: can't be used ({type(e).__name__})")
                provider = None
            if cache is not None and provider is not None:
                cache[row["id"]] = (_fingerprint(row), provider)
        inboxes[row["id"]] = Inbox(
            id=row["id"], address=row["address"],
            name=(row.get("display_name") or "").strip() or identity_from_config(config).display_name,
            provider=provider, cap=inbox_cold_cap(row, email_cfg, today),
            sent_today=await store.sent_since(row["id"], since, kind="sequence"),
            queued_first=queued.get(row["id"], 0),
            paused_reason=row.get("paused_reason") or "")
    pools = await store.campaign_pools() if len(inboxes) > 1 else {}
    return Inboxes(store=store, inboxes=inboxes, pools=pools, routed_mail=routed)
