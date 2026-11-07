"""Send hooks: rules an extension adds to the decision to send one email, and
headers it adds to it.

The sender's own checks (stop-on-reply, suppressions, do-not-contact, the
pre-send gate, the autopilot review) always run first. Then each registered
send rule may hold the email (it stays approved and is tried again next
cycle) or cancel it, with a reason the Outbox shows. A rule can never send
an email the built-in checks stopped.

A header hook returns extra headers for an email, such as List-Unsubscribe.
A provider that can't carry custom headers (Microsoft Graph sets its own) is
never handed an email that needs them: the email is held instead, so it
never goes out without them.

Register both through an extension (sellbird.extensions):

    def install(registry):
        registry.add_send_rule("myext.warm", warm_enough)
        registry.add_send_headers("myext.unsubscribe", unsubscribe_headers)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("sellbird.send_hooks")


@dataclass(frozen=True)
class SendCheck:
    """One email about to go out, as a send rule sees it."""
    item: dict          # the outbox row: to_email, subject, body, kind, step, ...
    prospect: Any       # the recipient's Prospect
    config: Any
    state: Any          # a StateManager

    @property
    def cold(self) -> bool:
        return self.item.get("kind") == "sequence"


@dataclass(frozen=True)
class Hold:
    """Not now: the email stays approved and is tried again next cycle."""
    reason: str


@dataclass(frozen=True)
class Cancel:
    """Never: the email is cancelled."""
    reason: str


SendRule = Callable[[SendCheck], Awaitable["Hold | Cancel | None"]]
HeaderHook = Callable[[dict], Iterable[tuple[str, str]]]

# Headers the provider and the sender own: a hook adding one is a bug.
RESERVED_HEADERS = frozenset({
    "from", "to", "cc", "bcc", "subject", "date", "message-id", "in-reply-to",
    "references", "reply-to", "sender", "return-path", "mime-version",
    "content-type", "content-transfer-encoding",
})
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,76}$")
NO_HEADERS_HOLD = "this inbox can't carry the headers this email needs"


class SendHookError(ValueError):
    """A hook returned something that can't be sent."""


async def first_verdict(rules: dict[str, SendRule], check: SendCheck) -> tuple[str, str] | None:
    """The first rule that holds or cancels, as (status, error) for the outbox,
    or None when every rule lets it through. A rule that raises holds the email:
    an extension's bug must never be what lets an email out."""
    for name, rule in rules.items():
        try:
            verdict = await rule(check)
        except Exception as e:
            logger.error(f"Send rule {name!r} failed; holding the email: {e}", exc_info=True)
            return "approved", f"held: {name} couldn't check it"
        if verdict is None:
            continue
        if isinstance(verdict, Cancel):
            return "cancelled", f"{name}: {verdict.reason}"[:300]
        if isinstance(verdict, Hold):
            return "approved", f"held: {name}: {verdict.reason}"[:300]
        logger.error(f"Send rule {name!r} returned {verdict!r}; holding the email.")
        return "approved", f"held: {name} gave no clear answer"
    return None


def extra_headers(hooks: dict[str, HeaderHook], item: dict) -> list[tuple[str, str]]:
    """Every hook's headers for this email, checked. Raises SendHookError on a
    reserved or malformed header, so the email is held rather than sent wrong."""
    out: list[tuple[str, str]] = []
    for name, hook in hooks.items():
        for header, value in hook(item) or ():
            header, value = str(header), str(value)
            if not _HEADER_NAME.match(header) or header.lower() in RESERVED_HEADERS:
                raise SendHookError(f"{name} added a header Sellbird can't send: {header!r}")
            if "\r" in value or "\n" in value:
                raise SendHookError(f"{name} added a header value with a line break: {header}")
            out.append((header, value))
    return out
