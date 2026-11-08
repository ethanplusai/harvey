"""Keep credentials out of Sellbird's log.

Some vendors take the API key in the query string (Hunter, Reoon,
ZeroBounce), and httpx logs every request URL at INFO. A log the dashboard
shows (data/sellbird.log) would put a key in a URL on screen. Two layers: httpx and httpcore only log warnings, and the
log file's handler masks any query value whose name looks like a secret.
"""

from __future__ import annotations

import logging
import re

# Loggers that print full request URLs at INFO/DEBUG.
NOISY_HTTP_LOGGERS = ("httpx", "httpcore")

# ?api_key=..., &apiKey=..., &token=..., &client_secret=..., &password=...
_SECRET_PARAM = re.compile(
    r"([?&][^=&\s#\"'<>]*(?:key|token|secret|password)[^=&\s#\"'<>]*=)[^&\s#\"'<>]+",
    re.IGNORECASE,
)

MASK = "***"


def redact(text: str) -> str:
    """Mask the values of secret-looking query parameters in text."""
    return _SECRET_PARAM.sub(lambda m: m.group(1) + MASK, text)


def quiet_http_loggers() -> None:
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class RedactSecrets(logging.Filter):
    """Rewrites each record so no secret query value reaches the handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # a broken record is logging's problem, not ours
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            # httpx errors carry the full URL in their text.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True
