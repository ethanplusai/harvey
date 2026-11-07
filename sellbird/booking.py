"""What the reply should offer, from the ``product.offer`` you configured.

``sellbird.yaml`` asks what happens when someone is interested: book a call
with your calendar link, suggest concrete times, ask for their preference,
start a trial, or just keep the conversation going. Until now only
``booking_url`` was read (the pre-send gate allows it); the rest was dead
config and every reply was told to "propose 2-3 specific times" whether or
not that fit the offer.

``booking_instruction`` turns the offer into instruction for the model. It
is never the email body itself: the caller puts the returned paragraph in
the handler prompt.
"""

from datetime import datetime, time, timedelta
from urllib.parse import urlparse

import pytz

from sellbird.config import operator_timezone, send_window_of

DEFAULT_START = time(9, 0)
DEFAULT_END = time(17, 0)
SUGGESTED_TIMES = 3
LOOKAHEAD_DAYS = 14


def _parse_clock(value, fallback: time) -> time:
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback


def _hours(config) -> tuple[time, time]:
    """The send window's hours, or sensible office hours when none is set.

    A reply is not held to the window, but a time inside it is a time the
    prospect is more likely to answer."""
    window = send_window_of(config)
    if window is None:
        return DEFAULT_START, DEFAULT_END
    start = _parse_clock(window.start, DEFAULT_START)
    end = _parse_clock(window.end, DEFAULT_END)
    return (start, end) if start < end else (DEFAULT_START, DEFAULT_END)


def _slot_times(config) -> list[time]:
    """One or two clock times inside the window, spread across the day."""
    start, end = _hours(config)
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute
    span = end_min - start_min
    if span >= 120:
        minutes = [start_min + 60, end_min - 60]
    elif span >= 60:
        minutes = [start_min + 30, end_min - 30]
    else:
        minutes = [start_min]
    out = []
    for value in minutes:
        slot = time(value // 60, value % 60)
        if slot not in out and start <= slot < end:
            out.append(slot)
    return out or [start]


def _local(now: datetime, zone: str) -> datetime:
    """``now`` on the recipient's clock. A naive ``now`` is taken as wall
    time in ``zone``, so tests and callers that already resolved the zone
    don't need to build an aware datetime."""
    if not zone:
        return now
    try:
        tz = pytz.timezone(zone)
    except pytz.UnknownTimeZoneError:
        return now
    if now.tzinfo is None:
        return tz.localize(now)
    return now.astimezone(tz)


def _format(day: datetime, at: time) -> str:
    # No %-d / %-I: those are not portable to Windows.
    clock = at.strftime("%I:%M %p")
    if clock.startswith("0"):
        clock = clock[1:]
    return f"{day:%A}, {day:%B} {day.day} at {clock}"


def _suggested_times(config, now: datetime) -> list[str]:
    """Two or three upcoming weekday slots, starting tomorrow."""
    window = send_window_of(config)
    weekdays_only = window.weekdays_only if window is not None else True
    times = _slot_times(config)
    day = now
    slots = []
    for offset in range(1, LOOKAHEAD_DAYS + 1):
        candidate = day + timedelta(days=offset)
        if weekdays_only and candidate.weekday() >= 5:
            continue
        slots.append(_format(candidate, times[len(slots) % len(times)]))
        if len(slots) == SUGGESTED_TIMES:
            break
    return slots


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"


def _valid_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return url


def _meeting(config) -> str:
    offer = getattr(getattr(config, "product", None), "offer", None)
    duration = str(getattr(offer, "meeting_duration", "") or "15 minutes").strip()
    owner = str(getattr(offer, "meeting_owner", "") or "").strip()
    if owner:
        return f"The call is {duration} with {owner}."
    return f"The call is {duration}."


def _time_instruction(config, now: datetime, zone: str, zone_known: bool) -> str:
    slots = _suggested_times(config, now)
    if not slots:
        return ("Ask what time works for them in the next week. Do not invent "
                "a specific time. " + _meeting(config))
    where = (f"These are already in the recipient's local time ({zone}); write "
             "them as given." if zone_known else
             "Write these times as given and do not name a time zone.")
    return (f"Propose these times: {_join(slots)}. {where} Offer no other "
            "time. " + _meeting(config))


def booking_instruction(config, now: datetime, zone: str = "") -> str:
    """Instruction for the next step this reply should offer.

    ``zone`` is the recipient's IANA time zone, when Sellbird knows it. With
    no zone the dates are worked out on the operator's clock and the text
    avoids naming one."""
    offer = getattr(getattr(config, "product", None), "offer", None)
    goal = str(getattr(offer, "goal", "") or "book_call")
    entry = str(getattr(offer, "entry", "") or "").strip()

    if goal == "get_reply":
        return ("Keep this reply conversational: answer them and ask one "
                "question about their situation. Do not propose a call, a "
                "meeting, a trial, or a link.")

    if goal == "start_trial" and entry:
        return (f"Offer this as the next step: {entry}. Keep it to one sentence "
                "and one action, and do not ask for a call or invent a "
                "different trial.")

    zone = str(zone or "").strip()
    local = _local(now, zone or operator_timezone(config))
    zone_known = bool(zone) and zone in pytz.all_timezones_set

    method = str(getattr(offer, "booking_method", "") or "calendar_link")
    url = _valid_url(getattr(offer, "booking_url", ""))
    if method == "calendar_link" and url:
        return (f"Offer this booking link: {url}\n"
                f"Let them pick a time on that page. Do not invent times or "
                f"offer to compare calendars yourself. {_meeting(config)}")
    if method == "ask_preference":
        return ("Ask what time works for them in the next week. Do not invent "
                "specific times or paste a link. " + _meeting(config))
    # ``suggest_times``, and the default ``calendar_link`` with no usable
    # URL: a link that can't be sent is worse than a real time.
    return _time_instruction(config, now=local, zone=zone, zone_known=zone_known)
