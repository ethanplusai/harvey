"""Dashboard translations for texts the API builds itself.

English is the source language and every English string is its own key. The
translations live in harvey/web/i18n/<lang>.json — the same files the browser
uses — so one dictionary covers both sides. The dashboard sends the viewer's
language in the X-Harvey-Lang header; anything without a translation falls
back to English.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parent / "web" / "i18n"
LANGUAGES = ("en", "de")


@lru_cache(maxsize=None)
def _dict(lang: str) -> dict[str, str]:
    if lang == "en":
        return {}
    try:
        return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def request_lang(request) -> str:
    """The viewer's dashboard language, from the X-Harvey-Lang header."""
    lang = (request.headers.get("x-harvey-lang") or "en").lower() if request else "en"
    return lang if lang in LANGUAGES else "en"


def tr(lang: str, text: str, **values) -> str:
    """Translate an English template, then fill its {name} placeholders."""
    out = _dict(lang).get(text) or text
    if values:
        out = re.sub(r"\{(\w+)\}",
                     lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
                     out)
    return out
