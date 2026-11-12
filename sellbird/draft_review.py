"""Rule checks on a draft before a human approves it.

Each check is deterministic and cheap, and returns plain-language flags a
reviewer can act on ("'10:00am PT' is 12:00pm in your call hours, outside
07:00-10:00"). Flags never block a send: they point at what to read twice.
The rules are the corrections reviewers kept making by hand: a missing
sign-off, the sender written about in the third person, call times outside
the hours the sender takes calls, dates in the wrong country's style,
phrasing that reads as non-native, and figures that aren't in the product
facts. Length and AI-sounding phrasing are sellbird.email_style's job.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE_ABBREVIATIONS = {
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "GMT": "Europe/London", "BST": "Europe/London", "UK TIME": "Europe/London",
    "UTC": "UTC",
}

NON_NATIVE_PHRASES = (
    "kindly", "do the needful", "revert back", "please find attached",
    "hope this email finds you well", "hope this finds you well",
    "greetings", "prepone",
)

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|"
           "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_ZONE = r"(?:\s*\(?\s*(" + "|".join(sorted(map(re.escape, ZONE_ABBREVIATIONS), key=len, reverse=True)) + r")\b)?"
_CLOCK = r"(1[0-2]|0?[1-9])(?::([0-5]\d))?"
_RANGE_RE = re.compile(_CLOCK + r"\s*(?:([ap])\.?m\.?)?\s*(?:-|–|—|to)\s*" + _CLOCK
                       + r"\s*([ap])\.?m\.?\b" + _ZONE, re.I)
_TIME_RE = re.compile(r"\b" + _CLOCK + r"\s*([ap])\.?m\.?\b" + _ZONE, re.I)
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_DAY_MONTH_RE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:" + _MONTHS + r")\b", re.I)
_FIGURE_RE = re.compile(
    r"(?<![\w.$])(\$?\d[\d,]*(?:\.\d+)?\s?(?:k|m|bn|b|%)\+?|\$\d[\d,]*(?:\.\d+)?|\d{1,3}(?:,\d{3})+\+?)(?![\w])",
    re.I)


def parse_call_hours(spec: str) -> tuple[int, int] | None:
    """"07:00-10:00" → minutes after midnight (420, 600), or None."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", str(spec or ""))
    if not m:
        return None
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    return (start, end) if 0 <= start < end <= 24 * 60 else None


def _minutes(hour: str, minute: str | None, meridiem: str) -> int:
    h = int(hour) % 12 + (12 if meridiem.lower() == "p" else 0)
    return h * 60 + int(minute or 0)


def _clock(minutes: int) -> str:
    h, m = divmod(minutes % (24 * 60), 60)
    return f"{(h % 12) or 12}:{m:02d}{'am' if h < 12 else 'pm'}"


def _in_zone(minutes: int, src: str, dst: str, day: datetime) -> int:
    """A wall-clock time in ``src`` on ``day``, as minutes after midnight in ``dst``."""
    at = datetime(day.year, day.month, day.day, minutes // 60, minutes % 60, tzinfo=ZoneInfo(src))
    local = at.astimezone(ZoneInfo(dst))
    return local.hour * 60 + local.minute


_CLOSINGS = re.compile(r"^(best|thanks|thank you|cheers|regards|best regards|kind regards|talk soon)\W*$", re.I)


def _split_sign_off(body: str, first_name: str, footer: str = "") -> tuple[str, str]:
    """(the body above the sign-off, the sign-off). The sign-off is the
    first of the last three non-empty lines that carries the sender's name,
    with a closing word ("Best,") just above it; "" when there is none."""
    text = body.rstrip()
    if footer and text.endswith(footer.strip()):
        text = text[: -len(footer.strip())].rstrip()
    lines = text.splitlines()
    idx = [i for i, line in enumerate(lines) if line.strip()]
    name = re.compile(r"\b" + re.escape(first_name) + r"\b", re.I) if first_name else None
    cut = next((i for i in idx[-3:]
                if name and name.search(lines[i]) and len(lines[i].split()) <= 6), None)
    if cut is None:
        return text, ""
    before = [i for i in idx if i < cut]
    if before and _CLOSINGS.match(lines[before[-1]].strip()):
        cut = before[-1]
    return "\n".join(lines[:cut]), "\n".join(lines[cut:])


def _figure_key(token: str) -> str:
    return re.sub(r"[\s+,]", "", token.lower())


def _time_flags(body: str, call_hours: str, call_zone: str, now: datetime) -> list[str]:
    flags: list[str] = []
    window = parse_call_hours(call_hours)
    try:
        ZoneInfo(call_zone) if call_zone else None
    except Exception:
        call_zone = ""

    def check(label: str, start: int, end: int, zone_label: str | None):
        if not zone_label:
            flags.append(f"'{label}' has no time zone; add one so the reader knows whose clock it is.")
            return
        if not window or not call_zone:
            return
        zone = ZONE_ABBREVIATIONS[zone_label.upper()]
        day = now.astimezone(ZoneInfo(zone))
        lo, hi = _in_zone(start, zone, call_zone, day), _in_zone(end, zone, call_zone, day)
        if lo < window[0] or hi > window[1] or hi < lo:
            flags.append(
                f"'{label}' is {_clock(lo)}" + (f"-{_clock(hi)}" if end != start else "")
                + f" in {call_zone}, outside your call hours ({call_hours}).")

    covered: list[tuple[int, int]] = []
    for m in _RANGE_RE.finditer(body):
        end_mer = m.group(6)
        start_mer = m.group(3) or end_mer
        start = _minutes(m.group(1), m.group(2), start_mer)
        end = _minutes(m.group(4), m.group(5), end_mer)
        if not m.group(3) and start > end:
            start = _minutes(m.group(1), m.group(2), "a" if end_mer.lower() == "p" else "p")
        check(m.group(0).strip(), start, end, m.group(7))
        covered.append(m.span())
    for m in _TIME_RE.finditer(body):
        if any(a <= m.start() < b for a, b in covered):
            continue
        t = _minutes(m.group(1), m.group(2), m.group(3))
        check(m.group(0).strip(), t, t, m.group(4))
    return flags


def review_draft(
    subject: str,
    body: str,
    *,
    sender_name: str = "",
    kind: str = "sequence",
    step: int = 1,
    country: str = "",
    call_hours: str = "",
    call_hours_timezone: str = "",
    facts: str = "",
    footer: str = "",
    now: datetime | None = None,
) -> list[str]:
    """Plain-language flags for one draft; [] when nothing stands out.

    ``country`` is the recipient's two-letter country when known (date
    style follows it); ``facts`` the text every figure must appear in
    (product config and knowledge); ``footer`` the compliance footer, which
    isn't part of what the writer wrote.
    """
    flags: list[str] = []
    now = now or datetime.now(ZoneInfo("UTC"))
    text = body or ""
    first = sender_name.split()[0] if sender_name and sender_name.split() else ""
    above, sign_off = _split_sign_off(text, first, footer)

    if first:
        if not sign_off:
            flags.append(f"No sign-off: end with your name ({first}).")
        if re.search(r"\b" + re.escape(first) + r"\b", above, re.I):
            flags.append(f"Mentions {first} by name above the sign-off. The email is from {first}: "
                         "write 'me' or 'I'.")

    flags += _time_flags(text, call_hours, call_hours_timezone, now)

    if _NUMERIC_DATE_RE.search(text):
        flags.append(f"'{_NUMERIC_DATE_RE.search(text).group(0)}' is ambiguous: "
                     "write the month as a word.")
    if country.upper() == "US":
        m = _DAY_MONTH_RE.search(text)
        if m:
            flags.append(f"'{m.group(0)}' is day-first; a US reader expects month-first ('Nov 12').")

    lowered = f"{subject}\n{text}".lower()
    for phrase in NON_NATIVE_PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"\b", lowered):
            flags.append(f"'{phrase}' reads as non-native to US and UK readers.")

    if facts:
        known = _figure_key(facts)
        seen: set[str] = set()
        for m in _FIGURE_RE.finditer(text):
            key = _figure_key(m.group(0))
            if key in seen:
                continue
            seen.add(key)
            if key.rstrip("+") not in known:
                flags.append(f"'{m.group(0).strip()}' isn't in your product facts: check the figure.")
    return flags


def product_facts(config, skills_dir=None) -> str:
    """Everything a figure in a draft may come from: the product config and
    the product knowledge skill."""
    from sellbird.paths import PROJECT_ROOT
    product = getattr(config, "product", None)
    parts: list[str] = []
    if product is not None:
        parts += [product.description, product.pricing, *product.key_benefits,
                  *product.objection_responses.keys(), *product.objection_responses.values()]
        offer = product.offer
        parts += [offer.primary, offer.entry, offer.meeting_duration]
    path = (skills_dir or PROJECT_ROOT / "skills") / "product_knowledge.md"
    try:
        parts.append(path.read_text(encoding="utf-8"))
    except OSError:
        pass
    return "\n".join(str(p) for p in parts if p)


def review_item(item: dict, config, *, country: str = "", facts: str | None = None,
                footer: str | None = None, now: datetime | None = None) -> list[str]:
    """``review_draft`` for an outbox row, with the settings from ``config``."""
    from sellbird.config import compliance_footer_lines
    offer = config.product.offer
    if footer is None:
        footer = "\n".join(compliance_footer_lines(config))
    return review_draft(
        item.get("subject") or "", item.get("body") or "",
        sender_name=config.persona.name,
        kind=item.get("kind") or "sequence",
        step=int(item.get("step") or 1),
        country=country,
        call_hours=offer.call_hours,
        call_hours_timezone=offer.call_hours_timezone,
        facts=product_facts(config) if facts is None else facts,
        footer=footer,
        now=now,
    )

