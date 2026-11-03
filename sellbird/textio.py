"""Text files and console output, the same on every platform.

Harvey reads and writes its files as UTF-8. Windows' default is the locale
code page (cp1252), so files saved there by an older Harvey, or edited in a
Windows tool, may not be UTF-8: those are still read, with a warning.
"""

import locale
import logging
import sys
from pathlib import Path

logger = logging.getLogger("harvey.textio")


def read_user_text(path) -> str:
    """A file a person or an older Harvey may have saved (harvey.yaml, .env,
    skills, prompts): UTF-8, with or without a BOM, else the locale's code
    page. Line endings come back as \n, as text-mode reads give them.
    Raises what reading the file raises (FileNotFoundError...)."""
    data = Path(path).read_bytes()
    try:
        return _newlines(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        # The system code page (cp1252 on most Windows), even in Python's
        # UTF-8 mode, where getpreferredencoding() would say utf-8.
        encoding = locale.getencoding()
        logger.warning(
            f"{path} isn't saved as UTF-8; read it as {encoding}. Re-save it as "
            "UTF-8 to keep characters like ★ or — intact.")
        return _newlines(data.decode(encoding, errors="replace"))


def _newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def utf8_stdio(*streams):
    """Write output as UTF-8. On Windows, output going to a file or pipe (the
    dashboard's log of `harvey run`) uses the locale code page, where "⚠" or
    "→" in a message would crash print(). stderr keeps backslash escapes, so
    odd bytes stay diagnosable."""
    for stream in streams or (sys.stdout, sys.stderr):
        errors = "backslashreplace" if stream is sys.stderr else "replace"
        try:
            stream.reconfigure(encoding="utf-8", errors=errors)
        except (AttributeError, ValueError, OSError):
            pass  # not a reconfigurable text stream (tests, embedded use)
