"""Keep credential query parameters out of log files.

Several vendors Harvey calls authenticate with an ``api_key`` query
parameter rather than a header (Hunter's email finder is the one that
surfaced this). httpx logs one full request URL per request at INFO —
query string included — so a plain ``basicConfig(level=INFO)`` writes
those keys into whatever log file the process keeps: not only the
heartbeat's own log, but the log of any standalone script that imports
harvey and configures logging itself.

:func:`install_log_redaction` — run when the ``harvey`` package is
imported, so every importer is covered with nothing to remember —
applies two layers:

1. :func:`quiet_httpx_loggers` drops the httpx/httpcore loggers to
   WARNING, which stops the routine per-request lines entirely while
   keeping real transport errors visible.
2. A wrapping log-record factory scrubs credential-looking query values
   from every record as it is created — whichever logger it came from
   and whichever handler ends up emitting it, including handlers a host
   script configures after importing harvey.

:class:`SecretRedactingFilter` is the same scrub as a ``logging.Filter``
for code that configures its own handlers: ``harvey.main`` attaches it
to the root handlers as well, so its lines stay covered even if
something replaces the record factory later.
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
# scrub has to be idempotent: a record passes through the factory and then
# one filter per handler).
_CREDENTIAL_QUERY = re.compile(
    r"(?i)(\b(?:api[-_]?key|access[-_]?token|client[-_]?secret|"
    r"password|passwd|secret|token|key)=)([^&\s'\"<>\[\]]+)"
)

REDACTED = "[REDACTED]"


def redact_query_secrets(text: str) -> str:
    """Return *text* with credential query values replaced by ``[REDACTED]``."""
    return _CREDENTIAL_QUERY.sub(lambda m: m.group(1) + REDACTED, text)


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Scrub *record* in place and return it."""
    message = record.getMessage()
    redacted = redact_query_secrets(message)
    if redacted != message:
        # Collapse args into the message: it is already fully formatted.
        record.msg = redacted
        record.args = None
    return record


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs credential query values from messages.

    Attached to the root handlers rather than to ``httpx`` alone, so it
    covers every logger that funnels through them. Idempotent: a record
    it has already redacted passes through unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


def quiet_httpx_loggers() -> None:
    """Drop the httpx/httpcore loggers from INFO to WARNING.

    Their INFO lines are exactly where the leak was: one
    ``HTTP Request: GET https://…?api_key=…`` per request.
    """
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


_installed = False


def install_log_redaction() -> None:
    """Install the redaction layers for the whole process. Idempotent.

    Quiet the httpx/httpcore loggers and wrap the log-record factory so
    every record is scrubbed at creation. A host script keeps working
    normally afterwards: its own ``basicConfig``, handlers and levels are
    untouched, and only credential query values change.
    """
    global _installed
    if _installed:
        return
    _installed = True
    quiet_httpx_loggers()
    previous_factory = logging.getLogRecordFactory()

    def redacting_record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        return _redact_record(previous_factory(*args, **kwargs))  # type: ignore[call-arg]

    logging.setLogRecordFactory(redacting_record_factory)
