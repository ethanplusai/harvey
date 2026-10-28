"""Keep credential query parameters out of Harvey's log files.

Several vendors Harvey calls authenticate with an ``api_key`` query
parameter rather than a header (Hunter's email finder is the one that
surfaced this). httpx logs one full request URL per request at INFO —
query string included — so a plain ``basicConfig(level=INFO)`` writes
those keys into the log file the heartbeat keeps. Two layers, applied
wherever Harvey configures logging (``harvey.main`` today):

1. :func:`quiet_httpx_loggers` drops the httpx/httpcore loggers to
   WARNING, which stops the routine per-request lines entirely while
   keeping real transport errors visible.
2. :class:`SecretRedactingFilter` scrubs credential-looking query values
   from any message that still carries one — an httpx error line, a
   logged exception, or a DEBUG line someone turns on while debugging,
   which would silently undo the quieting on its own.
"""

from __future__ import annotations

import logging
import re

# Query parameters whose value is a credential. Whole-word alternations
# (``\b``), so ``monkey=`` and ``keyboard=`` are left alone; case-insensitive
# so ``API_KEY`` and ``ApiKey`` are covered too. The value runs to the next
# ``&``, whitespace, or a quote/bracket, so the rest of a URL — and the
# parameter after the secret — survives. Brackets are excluded from the
# value so an already-redacted ``[REDACTED]`` can never match again (the
# filter has to be idempotent: records pass through one filter per handler).
_CREDENTIAL_QUERY = re.compile(
    r"(?i)(\b(?:api[-_]?key|access[-_]?token|client[-_]?secret|"
    r"password|passwd|secret|token|key)=)([^&\s'\"<>\[\]]+)"
)

REDACTED = "[REDACTED]"


def redact_query_secrets(text: str) -> str:
    """Return *text* with credential query values replaced by ``[REDACTED]``."""
    return _CREDENTIAL_QUERY.sub(lambda m: m.group(1) + REDACTED, text)


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs credential query values from messages.

    Attached to the root handlers rather than to ``httpx`` alone, so it
    covers every logger that funnels through them. Idempotent: a record
    it has already redacted passes through unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_query_secrets(message)
        if redacted != message:
            # Collapse args into the message: it is already fully formatted.
            record.msg = redacted
            record.args = None
        return True


def quiet_httpx_loggers() -> None:
    """Drop the httpx/httpcore loggers from INFO to WARNING.

    Their INFO lines are exactly where the leak was: one
    ``HTTP Request: GET https://…?api_key=…`` per request.
    """
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
