"""Phone and WhatsApp numbers a business publishes on its own site.

Only numbers the site itself gives out: `tel:` links, WhatsApp chat links
(wa.me, api.whatsapp.com), schema.org `telephone`, and numbers printed with
an international prefix. Nothing is guessed.
"""

import re
from urllib.parse import parse_qs, unquote, urlparse

from harvey.collectors.people import _json_blocks, _nodes

# Country calling codes for the places a number's country can be read from
# (a location or a country-code domain). Mobile prefixes follow the code.
COUNTRIES = {
    "971": {"names": ("uae", "united arab emirates", "dubai", "abu dhabi", "sharjah", "ajman",
                      "ras al khaimah", "fujairah", "umm al quwain"), "tld": "ae", "mobile": ("5",)},
    "966": {"names": ("saudi", "ksa", "riyadh", "jeddah", "dammam", "khobar", "mecca", "medina"),
            "tld": "sa", "mobile": ("5",)},
    "974": {"names": ("qatar", "doha"), "tld": "qa", "mobile": ("3", "5", "6", "7")},
    "965": {"names": ("kuwait",), "tld": "kw", "mobile": ("5", "6", "9")},
    "973": {"names": ("bahrain", "manama"), "tld": "bh", "mobile": ("3",)},
    "968": {"names": ("oman", "muscat"), "tld": "om", "mobile": ("7", "9")},
}

# Printed numbers count only with an international prefix (+971 / 00971):
# a bare run of digits on a page is as often an order or licence number.
_PRINTED = re.compile(r"(?<![\w+])(?:\+|00)\s?(9(?:71|66|74|65|73|68))[\s.\-()]*(\d[\d\s.\-()]{6,14}\d)")
_TAGS = re.compile(r"(?s)<(script|style)\b.*?</\1>|<[^>]+>")


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def country_code(*hints: str) -> str:
    """The calling code a location or domain points to, or ''."""
    for hint in hints:
        h = (hint or "").lower()
        if not h:
            continue
        for code, c in COUNTRIES.items():
            if h.rstrip("/").endswith("." + c["tld"]) or any(n in h for n in c["names"]):
                return code
    return ""


def international(number: str, *hints: str) -> str:
    """Digits in international form (971500000000), or '' when the country
    can't be told. A local 0-number takes its country from the hints."""
    raw = (number or "").strip().replace("(0)", "")
    d = _digits(raw)
    if not d:
        return ""
    if raw.startswith("+") or d.startswith("00"):
        d = d[2:] if d.startswith("00") else d
        for code in COUNTRIES:
            if d.startswith(code + "0"):    # +971 050... written with the trunk 0
                d = code + d[len(code) + 1:]
        return d if len(d) >= 8 else ""
    if any(d.startswith(code) for code in COUNTRIES) and len(d) >= 11:
        return d
    code = country_code(*hints)
    if code and d.startswith("0") and len(d) >= 8:
        return code + d[1:]
    if code and len(d) in (8, 9):
        return code + d
    return ""


def is_mobile(intl: str) -> bool:
    """True for a Gulf mobile number: the kind WhatsApp runs on."""
    for code, c in COUNTRIES.items():
        if intl.startswith(code):
            return intl[len(code):].startswith(c["mobile"])
    return False


def pretty(intl: str) -> str:
    """+971 50 000 0000 style, for reading out loud."""
    if not intl:
        return ""
    for code in COUNTRIES:
        if intl.startswith(code):
            rest = intl[len(code):]
            return f"+{code} {rest[:2]} {rest[2:5]} {rest[5:]}".strip()
    return "+" + intl


def _whatsapp_number(href: str) -> str:
    u = urlparse(href.strip())
    host = (u.netloc or "").lower()
    if host.endswith("wa.me"):
        return _digits(u.path.strip("/").split("/")[0])
    if "whatsapp" in host or u.scheme == "whatsapp":
        return _digits((parse_qs(u.query).get("phone") or [""])[0])
    return ""


def numbers_on_page(html: str) -> tuple[list[str], list[str]]:
    """(phone numbers, WhatsApp numbers) published on one page, as found."""
    phones: list[str] = []
    whatsapp: list[str] = []
    for href in re.findall(r'(?i)href\s*=\s*["\']([^"\']+)["\']', html or ""):
        href = unquote(href)
        if href.lower().startswith("tel:"):
            num = href[4:].split("?")[0].strip()
            if len(_digits(num)) >= 7:
                phones.append(num)
        else:
            wa = _whatsapp_number(href)
            if len(wa) >= 8:
                whatsapp.append("+" + wa)
    for block in _json_blocks(html):
        for node in _nodes(block):
            tel = node.get("telephone")
            for t in (tel if isinstance(tel, list) else [tel]):
                if isinstance(t, str) and len(_digits(t)) >= 7:
                    phones.append(t.strip())
    text = _TAGS.sub(" ", html or "")
    for code, rest in _PRINTED.findall(text):
        phones.append(f"+{code} {rest.strip()}")
    return phones, whatsapp


def contact_numbers(pages: list[tuple[str, str]], *hints: str) -> dict:
    """The business's best phone and WhatsApp number across its pages, in
    international digits ('' when none). A mobile number counts as the
    phone first, since that's the one a person answers and has WhatsApp."""
    phones: list[str] = []
    whatsapp: list[str] = []
    for _, html in pages:
        p, w = numbers_on_page(html)
        phones += p
        whatsapp += w

    def unique(found: list[str]) -> list[str]:
        out: list[str] = []
        for n in found:
            intl = international(n, *hints)
            if intl and intl not in out:
                out.append(intl)
        return out

    phones_i, wa_i = unique(phones), unique(whatsapp)
    phone = next((n for n in phones_i if is_mobile(n)), phones_i[0] if phones_i else "")
    return {"phone": phone, "whatsapp": wa_i[0] if wa_i else ""}
