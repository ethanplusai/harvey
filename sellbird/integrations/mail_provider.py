"""Mail provider abstraction — Harvey owns its sending, Instantly optional.

A MailProvider gives Harvey full inbox control: send, read replies,
thread. Two native providers ship:

- ``gmail``  — a Google Workspace / Gmail mailbox via the Gmail REST API
  (recommended: a real mailbox on a dedicated secondary domain is the
  most deliverable way to send <50 cold emails/day).
- ``microsoft`` — a Microsoft 365 / Outlook mailbox via Microsoft Graph
  (OAuth; Exchange Online no longer accepts passwords over IMAP).
- ``smtp``   — any mailbox that speaks SMTP + IMAP (AgentMail, Fastmail,
  a Workspace app-mailbox, self-hosted). Configuration via .env.

``instantly`` remains supported through the legacy Sender/Handler path —
it isn't a MailProvider because Instantly owns sequencing/sending itself.
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("harvey.mail")

NATIVE_PROVIDERS = ("gmail", "smtp", "microsoft")


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""     # RFC Message-ID (or provider id) of the sent mail
    thread_ref: str = ""     # provider thread handle (Gmail threadId, etc.)
    error: str = ""


@dataclass
class InboundMessage:
    provider_id: str         # provider-unique id, used for reply dedup
    from_email: str
    subject: str = ""
    body: str = ""
    message_id: str = ""     # RFC Message-ID header
    in_reply_to: str = ""    # RFC In-Reply-To header (bounce/thread matching)
    thread_ref: str = ""
    date: str = ""
    is_bounce: bool = False
    headers: dict = field(default_factory=dict)


BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
BOUNCE_SUBJECTS = (
    "delivery status notification", "undeliverable", "returned mail",
    "delivery failure", "failure notice", "could not be delivered",
    "delivery incomplete", "address not found",
)


# Where a reply starts quoting the email it answers. Clients wrap the
# "On ... wrote:" line, so one line break is allowed inside it.
# After these, the quoted email is usually marked with ">", so any unmarked
# lines under them are the person's own inline answers.
_ATTRIBUTION_HEADERS = (
    # Gmail / Apple Mail, and the Spanish, German and French equivalents
    re.compile(r"^[ \t]*On\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bwrote:[ \t]*$", re.MULTILINE),
    re.compile(r"^[ \t]*El\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bescribi[oó]:[ \t]*$", re.MULTILINE),
    re.compile(r"^[ \t]*Am\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bschrieb[^\n]{0,100}:[ \t]*$", re.MULTILINE),
    re.compile(r"^[ \t]*Le\b[^\n]{0,300}(?:\n[^\n]{0,300})?\ba [ée]crit[ \t]*:[ \t]*$", re.MULTILINE),
)
# Outlook-style blocks: the quoted email follows unmarked, so everything
# from here on is cut.
_BLOCK_HEADERS = (
    re.compile(r"^[ \t]*-{2,}[ \t]*Original Message[ \t]*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^[ \t]*(?:_{10,}[ \t]*\n[ \t]*)?(?:From|Von|De):[^\n]*\n[ \t]*"
               r"(?:Sent|Date|Gesendet|Enviado|Envoy[ée]):", re.MULTILINE),
)


def _unquoted(text: str) -> str:
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith(">"))


def strip_quoted_reply(body: str) -> str:
    """What the sender wrote, without the email they're answering.

    ">" lines are dropped wherever they are. An "On ... wrote:" line is
    dropped too, and the unmarked lines under it are kept when the quote
    below it is ">"-marked (inline answers); when it isn't, everything from
    it on is the quote. An Outlook From:/Sent: block cuts the rest. Never
    returns nothing for a non-empty reply. The Handler also drops any line
    that repeats what Harvey sent, which catches quotes no header marks.
    """
    text = (body or "").replace("\r\n", "\n")
    cut = len(text)
    for pattern in _BLOCK_HEADERS:
        m = pattern.search(text)
        if m and m.start() < cut:
            cut = m.start()
    head, tail = text[:cut], ""
    for pattern in _ATTRIBUTION_HEADERS:
        m = pattern.search(head)
        if not m:
            continue
        below = head[m.end():]
        marked = any(l.lstrip().startswith(">") for l in below.split("\n"))
        head = head[:m.start()] + ("\n" + below if marked else "")
    kept = _unquoted(head).strip() or _unquoted(text).strip()
    return re.sub(r"\n{3,}", "\n\n", kept)


def looks_like_bounce(from_email: str, subject: str) -> bool:
    f = (from_email or "").lower()
    s = (subject or "").lower()
    return any(m in f for m in BOUNCE_SENDERS) or any(m in s for m in BOUNCE_SUBJECTS)


class MailProvider(ABC):
    """A mailbox Harvey can send from and read replies out of."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether required credentials are present (no network)."""

    @abstractmethod
    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
    ) -> SendResult:
        """Send one plain-text email. Reply threading when refs are given."""

    @abstractmethod
    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """Fetch recent inbound messages (dedup is the caller's job)."""

    def confirm_read(self) -> None:
        """Every reply the last get_replies returned was handled. A provider
        that reads incrementally (Gmail's history) moves on from them only
        now; the others need do nothing."""

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Cheap end-to-end credential check. Returns (ok, detail)."""


def get_mail_provider(config, env) -> MailProvider | None:
    """Build the configured native provider, or None for instantly/unknown."""
    provider = (config.channels.email.provider or "").strip().lower()
    if provider == "gmail":
        from harvey.integrations.gmail import GmailProvider
        return GmailProvider(config, env)
    if provider == "smtp":
        from harvey.integrations.smtp_mail import SmtpImapProvider
        return SmtpImapProvider(config, env)
    if provider == "microsoft":
        from harvey.integrations.microsoft import MicrosoftProvider
        return MicrosoftProvider(config, env)
    return None
