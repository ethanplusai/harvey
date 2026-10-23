"""Send windows in the recipient's own time zone.

With ``channels.email.send_window`` set, a cold email goes out only inside
that window (08:00 to 17:00 on weekdays by default) in the recipient's local
time, so a campaign that reaches New York and Oslo lands in office hours in
both. Replies are never held.

The recipient's zone comes from what Harvey already knows, in this order:

1. the company's location: a US state ("Austin, TX", "Brooklyn, New York"),
   a Canadian province or Australian state ("Toronto, ON, Canada",
   "Sydney NSW 2000"), or a country with a single time zone ("Oslo, Norway");
2. the coordinates discovery cached when it geocoded that location (the
   ``geo:<place>`` settings), for a country with several zones or a place
   given without its country;
3. the recipient's country as Precleared worked it out from the address,
   company records or the company's website, when the optional compliance
   check is on;
4. ``compliance.precleared.prospect_country``.

A country with several zones and nothing more specific stays unresolved, as
does an empty location. Those recipients get the window in
``usage.quiet_hours.timezone``, which is what Harvey did before. A resolved
zone is stored on the prospect, so it is worked out once.

Two-letter codes follow the US convention: "Berlin, DE" reads as Delaware
unless the cached coordinates or Precleared's country say Germany. Writing
the country out ("Berlin, Germany") always works.
"""

import math
import random
import re
import unicodedata
from datetime import datetime, timedelta, time
from functools import lru_cache

import pytz

# Up to this much is added to a window's opening time when an email is moved
# to the next window, so a batch doesn't all go out on the same minute.
START_SPREAD = timedelta(minutes=30)

# Coordinates with no known country pick the nearest zone only if its
# reference city is this close; anything further is left unresolved.
MAX_NEAREST_KM = 500

# A state or province read from the text is dropped when the cached
# coordinates put the place further than this from its zone's reference
# city ("Perth, WA" geocoded to Australia is not Washington state).
MAX_REGION_KM = 3000

# Majority zone per state. A few states span two zones; the one most of
# their population lives in wins.
US_STATES = {
    "AL": ("alabama", "America/Chicago"),
    "AK": ("alaska", "America/Anchorage"),
    "AZ": ("arizona", "America/Phoenix"),
    "AR": ("arkansas", "America/Chicago"),
    "CA": ("california", "America/Los_Angeles"),
    "CO": ("colorado", "America/Denver"),
    "CT": ("connecticut", "America/New_York"),
    "DE": ("delaware", "America/New_York"),
    "DC": ("district of columbia", "America/New_York"),
    "FL": ("florida", "America/New_York"),
    "GA": ("georgia", "America/New_York"),
    "HI": ("hawaii", "Pacific/Honolulu"),
    "ID": ("idaho", "America/Boise"),
    "IL": ("illinois", "America/Chicago"),
    "IN": ("indiana", "America/Indiana/Indianapolis"),
    "IA": ("iowa", "America/Chicago"),
    "KS": ("kansas", "America/Chicago"),
    "KY": ("kentucky", "America/New_York"),
    "LA": ("louisiana", "America/Chicago"),
    "ME": ("maine", "America/New_York"),
    "MD": ("maryland", "America/New_York"),
    "MA": ("massachusetts", "America/New_York"),
    "MI": ("michigan", "America/Detroit"),
    "MN": ("minnesota", "America/Chicago"),
    "MS": ("mississippi", "America/Chicago"),
    "MO": ("missouri", "America/Chicago"),
    "MT": ("montana", "America/Denver"),
    "NE": ("nebraska", "America/Chicago"),
    "NV": ("nevada", "America/Los_Angeles"),
    "NH": ("new hampshire", "America/New_York"),
    "NJ": ("new jersey", "America/New_York"),
    "NM": ("new mexico", "America/Denver"),
    "NY": ("new york", "America/New_York"),
    "NC": ("north carolina", "America/New_York"),
    "ND": ("north dakota", "America/Chicago"),
    "OH": ("ohio", "America/New_York"),
    "OK": ("oklahoma", "America/Chicago"),
    "OR": ("oregon", "America/Los_Angeles"),
    "PA": ("pennsylvania", "America/New_York"),
    "RI": ("rhode island", "America/New_York"),
    "SC": ("south carolina", "America/New_York"),
    "SD": ("south dakota", "America/Chicago"),
    "TN": ("tennessee", "America/Chicago"),
    "TX": ("texas", "America/Chicago"),
    "UT": ("utah", "America/Denver"),
    "VT": ("vermont", "America/New_York"),
    "VA": ("virginia", "America/New_York"),
    "WA": ("washington", "America/Los_Angeles"),
    "WV": ("west virginia", "America/New_York"),
    "WI": ("wisconsin", "America/Chicago"),
    "WY": ("wyoming", "America/Denver"),
}

# Some province and state codes clash with country codes (NL is the
# Netherlands as well as Newfoundland), so those only count next to their
# country. Full names always count.
CA_PROVINCES = {
    "AB": ("alberta", "America/Edmonton"),
    "BC": ("british columbia", "America/Vancouver"),
    "MB": ("manitoba", "America/Winnipeg"),
    "NB": ("new brunswick", "America/Moncton"),
    "NL": ("newfoundland and labrador", "America/St_Johns"),
    "NS": ("nova scotia", "America/Halifax"),
    "NT": ("northwest territories", "America/Edmonton"),
    "NU": ("nunavut", "America/Iqaluit"),
    "ON": ("ontario", "America/Toronto"),
    "PE": ("prince edward island", "America/Halifax"),
    "QC": ("quebec", "America/Toronto"),
    "SK": ("saskatchewan", "America/Regina"),
    "YT": ("yukon", "America/Whitehorse"),
}
AU_STATES = {
    "NSW": ("new south wales", "Australia/Sydney"),
    "VIC": ("victoria", "Australia/Melbourne"),
    "QLD": ("queensland", "Australia/Brisbane"),
    "WA": ("western australia", "Australia/Perth"),
    "SA": ("south australia", "Australia/Adelaide"),
    "TAS": ("tasmania", "Australia/Hobart"),
    "ACT": ("australian capital territory", "Australia/Sydney"),
    "NT": ("northern territory", "Australia/Darwin"),
}
# Codes that clash with nothing else count without their country.
CA_CODES_ALONE = {"AB", "BC", "MB", "NB", "NS", "ON", "QC"}
AU_CODES_ALONE = {"NSW", "VIC", "QLD", "TAS", "ACT"}

# Countries whose other zones are only small islands or regions: the
# mainland zone is the sensible answer for a company there.
MAIN_ZONE = {
    "CL": "America/Santiago",
    "CN": "Asia/Shanghai",
    "EC": "America/Guayaquil",
    "ES": "Europe/Madrid",
    "NZ": "Pacific/Auckland",
    "PT": "Europe/Lisbon",
    "UA": "Europe/Kyiv",
}

COUNTRY_ALIASES = {
    "uk": "GB", "united kingdom": "GB", "great britain": "GB", "britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "usa": "US", "us": "US", "united states of america": "US", "america": "US",
    "the netherlands": "NL", "holland": "NL", "deutschland": "DE",
    "south korea": "KR", "korea": "KR", "uae": "AE", "czechia": "CZ",
    "republic of ireland": "IE", "eire": "IE",
}
# Other spellings of provinces and states, mapped to their codes.
REGION_ALIASES = {"newfoundland": "NL", "labrador": "NL", "pei": "PE", "yukon territory": "YT"}


def _clean(text: str) -> str:
    """Lowercase, no accents or dots, single spaces: "Québec" -> "quebec"."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.replace(".", "").lower().split())


@lru_cache(maxsize=1)
def _country_names() -> dict[str, str]:
    states = {name for name, _ in US_STATES.values()}
    # "Atlanta, Georgia" is the state; cached coordinates catch Tbilisi.
    names = {_clean(name): code for code, name in pytz.country_names.items()
             if _clean(name) not in states}
    names.update(COUNTRY_ALIASES)
    return names


@lru_cache(maxsize=1)
def _zone_points() -> dict[str, tuple[float, float]]:
    """Each zone's reference city, from the tz database's zone.tab."""
    points = {}
    with pytz.open_resource("zone.tab") as f:
        for line in f.read().decode("utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 3:
                point = _iso6709(fields[1])
                if point:
                    points[fields[2]] = point
    return points


def _iso6709(text: str) -> tuple[float, float] | None:
    """"+5955+01045" or "+404251-0740023" -> (lat, lon) in degrees."""
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?([+-])(\d{3})(\d{2})(\d{2})?", text)
    if not m:
        return None
    s1, d1, m1, x1, s2, d2, m2, x2 = m.groups()
    lat = int(d1) + int(m1) / 60 + int(x1 or 0) / 3600
    lon = int(d2) + int(m2) / 60 + int(x2 or 0) / 3600
    return (lat if s1 == "+" else -lat, lon if s2 == "+" else -lon)


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _nearest(point, zones=None, max_km: float | None = None) -> str:
    """The zone whose reference city is closest to ``point``, or ""."""
    refs = _zone_points()
    candidates = [z for z in (zones if zones is not None else refs) if z in refs]
    if not point or not candidates:
        return ""
    best = min(candidates, key=lambda z: _km(point, refs[z]))
    if max_km is not None and _km(point, refs[best]) > max_km:
        return ""
    return best


def _point(text: str) -> tuple[float, float] | None:
    """The "lat,lon" (optionally ",radius") discovery caches, or None."""
    try:
        lat, lon = (float(x) for x in str(text or "").split(",")[:2])
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return (lat, lon)


def country_code(value) -> str:
    """A two-letter country code the tz database knows ("uk" -> "GB"), or ""."""
    code = str(value or "").strip().upper()
    code = {"UK": "GB", "USA": "US"}.get(code, code)
    return code if code in pytz.country_timezones else ""


def _one_offset(zones: list[str]) -> bool:
    """True when every zone keeps the same clock all year (Berlin and
    Busingen), so the country effectively has one zone."""
    year = datetime.now().year
    seen = {
        tuple(pytz.timezone(z).utcoffset(datetime(year, month, 15, 12)) for month in (1, 7))
        for z in zones
    }
    return len(seen) == 1


def _country_zone(code: str, point=None) -> str:
    zones = pytz.country_timezones.get(code, [])
    if not zones:
        return ""
    if len(zones) == 1 or _one_offset(zones):
        return zones[0]
    if point:
        return _nearest(point, zones)
    return MAIN_ZONE.get(code, "")


def _parts(location: str) -> list[str]:
    """Comma-separated parts, last first, with postcodes dropped."""
    parts = []
    for raw in reversed(str(location or "").split(",")):
        part = re.sub(r"\b[\dA-Z]*\d[\dA-Z-]*\b", " ", raw)  # 78701, 2000, EC1A
        part = " ".join(part.split())
        if part:
            parts.append(part)
    return parts


def _country_in(parts: list[str]) -> str:
    names = _country_names()
    for part in parts:
        name = _clean(part)
        if name in names:
            return names[name]
        # A bare two-letter code is a US state first ("Wilmington, DE").
        if len(part) == 2 and part.isupper() and part not in US_STATES:
            found = country_code(part)
            if found:
                return found
    return ""


def _region(parts: list[str], text_country: str, hint: str) -> str:
    """The zone of a US state, Canadian province or Australian state named
    in the location, or "".

    ``text_country`` is a country written in the location itself, ``hint``
    one known from elsewhere. A two-letter code that is both a US state and
    a country ("DE", "IN") is read as the state, unless the hint says it's
    that country.
    """
    context = text_country or hint
    us_names = {name: zone for name, zone in US_STATES.values()}
    for part in parts:
        name = _clean(part)
        codes = [w for w in part.split() if w.isupper()]
        if text_country in ("", "US"):
            if name in us_names:
                return us_names[name]
            for code in codes:
                if code in US_STATES and not (
                        hint and hint != "US" and country_code(code) == hint):
                    return US_STATES[code][1]
        if context in ("", "CA"):
            code = REGION_ALIASES.get(name, "")
            for key, (full, zone) in CA_PROVINCES.items():
                if name == full or code == key or (
                        key in codes and (context == "CA" or key in CA_CODES_ALONE)):
                    return zone
        if context in ("", "AU"):
            for key, (full, zone) in AU_STATES.items():
                if name == full or (key in codes and (context == "AU" or key in AU_CODES_ALONE)):
                    return zone
    return ""


def zone_for(location: str = "", country: str = "", point: str = "") -> str:
    """The IANA time zone for a recipient, or "" when it can't be told.

    ``location`` is free text such as a company's address or "Denver, CO";
    ``country`` a two-letter code known from elsewhere (Precleared's answer
    or the configured prospect country); ``point`` the "lat,lon" discovery
    cached for the location. A state or country written in the location
    wins over ``country``.
    """
    parts = _parts(location)
    coords = _point(point)
    text_country = _country_in(parts)
    hint = country_code(country)
    zone = _region(parts, text_country, hint)
    if zone and coords and _km(coords, _zone_points().get(zone, coords)) > MAX_REGION_KM:
        # The coordinates put it on another continent ("Perth, WA" in
        # Australia), so the state was a misreading.
        zone = ""
        if text_country == "US":
            text_country = ""
    if zone:
        return zone
    named = text_country or hint
    if named:
        return _country_zone(named, coords)
    if coords:
        return _nearest(coords, max_km=MAX_NEAREST_KM)
    return ""


# ── The window itself ──

def _clock(value: str) -> time:
    return time.fromisoformat(value)


def _localize(tz, naive: datetime) -> datetime:
    """A local wall-clock time as an aware datetime, through DST changes:
    a time skipped in spring becomes the moment after the gap, and a time
    that happens twice in autumn is the first of the two."""
    try:
        return tz.localize(naive, is_dst=None)
    except pytz.AmbiguousTimeError:
        return tz.localize(naive, is_dst=True)
    except pytz.NonExistentTimeError:
        return tz.normalize(tz.localize(naive, is_dst=False))


def _bounds(tz, day, window) -> tuple[datetime, datetime]:
    return (_localize(tz, datetime.combine(day, _clock(window.start))),
            _localize(tz, datetime.combine(day, _clock(window.end))))


def in_window(at: datetime, zone: str, window) -> bool:
    """Whether ``at`` (naive UTC) falls inside the window in ``zone``."""
    tz = pytz.timezone(zone)
    local = pytz.utc.localize(at).astimezone(tz)
    if window.weekdays_only and local.weekday() >= 5:
        return False
    opens, closes = _bounds(tz, local.date(), window)
    return opens <= local < closes


def next_send_time(after: datetime, zone: str, window, rng=None) -> datetime:
    """The first moment at or after ``after`` (naive UTC) inside the window
    in ``zone``, as naive UTC. Inside the window already: ``after`` itself.
    Otherwise the next opening, plus up to START_SPREAD so a batch moved to
    the same morning doesn't all go at once."""
    tz = pytz.timezone(zone)
    local = pytz.utc.localize(after).astimezone(tz)
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        if window.weekdays_only and day.weekday() >= 5:
            continue
        opens, closes = _bounds(tz, day, window)
        if local >= closes:
            continue
        if local >= opens:
            return after
        spread = min(START_SPREAD, (closes - opens) / 4).total_seconds()
        when = opens + timedelta(seconds=(rng or random).uniform(0, spread))
        return when.astimezone(pytz.utc).replace(tzinfo=None)
    raise ValueError("send window never opens")  # start < end is validated


# ── Per-recipient lookup ──

def _prospect_country(config) -> str:
    compliance = getattr(config, "compliance", None)
    precleared = getattr(compliance, "precleared", None)
    return country_code(getattr(precleared, "prospect_country", ""))


async def recipient_zone(state, prospect, config, country: str = "") -> str:
    """This recipient's zone, worked out once and stored on the prospect.

    ``country`` is the recipient's country from Precleared, when a check
    returned one. Returns "" when nothing settles it."""
    if prospect is None:
        return ""
    if getattr(prospect, "timezone", ""):
        return prospect.timezone
    location = ""
    if getattr(prospect, "company_id", ""):
        company = await state.get_company(prospect.company_id)
        location = (getattr(company, "location", "") or "") if company else ""
    point = ""
    if location.strip():
        cached = await state.get_setting("geo:" + location.strip().lower())
        point = "" if not cached or cached == "-" else cached
    hint = country_code(country) or _prospect_country(config)
    zone = zone_for(location, country=hint, point=point)
    if zone:
        prospect.timezone = zone
        await state.set_prospect_timezone(prospect.id, zone)
    return zone
