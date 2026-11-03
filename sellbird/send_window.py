"""Send windows in the recipient's own time zone.

With ``channels.email.send_window`` set, a cold email goes out only inside
that window (08:00 to 17:00 on weekdays by default) in the recipient's local
time, so a campaign that reaches New York and Oslo lands in office hours in
both. Replies are never held by the window; in quiet hours they wait for
the operator's own hours, as before. ``weekdays_only`` means Monday to
Friday everywhere.

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

A country with several zones and nothing more specific gets its main zone
(MAIN_ZONE: Sydney for Australia, Moscow for Russia), except the US, which
has none. A main zone is used but not stored, so coordinates or a country
found later can still correct it. An empty location stays unresolved. Those recipients get the window in
``usage.quiet_hours.timezone``, which is what Harvey did before. A resolved
zone is stored on the prospect, so it is worked out once (unless a country
found later could still change it; see ``recipient_zone``).

A code that is a state or province and a country too ("Berlin, DE",
"Mumbai, IN", "St. John's, NL", "Adelaide, SA") is settled by Precleared's
country, the cached coordinates or ``prospect_country``. With none of
those, a US state code reads as the state and any other as the country, and
that guess isn't stored. Writing the country out ("Berlin, Germany") always
works.

The same reading gives Precleared the recipient's country, when the check
is on (``recipient_country``). Only a country Harvey is sure of goes: one
named in the location, the country of a state or province named there, a
code that is a region and a country too once nearby cached coordinates settle
it. A stored time zone alone has no country provenance and does not count.
A code read by convention alone, a state reading the coordinates
contradict, and coordinates by themselves (the nearest reference city can
be over a border) send nothing, because Precleared takes the country it is
given as settled, and a wrong one is worse than none. Without one,
``prospect_country`` or the one country every ``icp.geography`` entry is in
goes as a default Precleared uses only when its own lookups show nothing
(``icp_country``).
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
# mainland zone is the sensible answer for a company there. Below them,
# countries with several zones where most people and businesses share one:
# used only when nothing more precise (a state, coordinates) is known. The
# US has no such zone (Eastern is under half), so it stays with the
# operator's own zone.
MAIN_ZONE = {
    "CL": "America/Santiago",
    "CN": "Asia/Shanghai",
    "EC": "America/Guayaquil",
    "ES": "Europe/Madrid",
    "NZ": "Pacific/Auckland",
    "PT": "Europe/Lisbon",
    "UA": "Europe/Kyiv",
    "AU": "Australia/Sydney",
    "BR": "America/Sao_Paulo",
    "CA": "America/Toronto",
    "ID": "Asia/Jakarta",
    "MX": "America/Mexico_City",
    "RU": "Europe/Moscow",
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


@lru_cache(maxsize=1)
def _zone_countries() -> dict[str, str]:
    """Each zone's country, from the tz database: no zone is listed under two."""
    return {zone: code for code, zones in pytz.country_timezones.items() for zone in zones}


def zone_country(zone: str) -> str:
    """The country a zone belongs to ("Europe/London" -> "GB"), or ""."""
    return _zone_countries().get(str(zone or ""), "")


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


def _main_zone_only(code: str, point=None) -> bool:
    """True when _country_zone can only give MAIN_ZONE's answer."""
    zones = pytz.country_timezones.get(code, [])
    return not point and len(zones) > 1 and not _one_offset(zones)


def _parts(location: str) -> list[str]:
    """Comma-separated parts, last first, with postcodes dropped."""
    parts = []
    for raw in reversed(str(location or "").split(",")):
        part = re.sub(r"\b[\dA-Z]*\d[\dA-Z-]*\b", " ", raw)  # 78701, 2000, EC1A
        part = " ".join(part.split())
        if part:
            parts.append(part)
    return parts


# Region codes by the country they're in. Some are a country's code too
# ("DE", "IN", "NL", "SA"): see _ambiguous_code.
REGION_COUNTRY = {
    **{code: "US" for code in US_STATES},
    **{code: "CA" for code in CA_PROVINCES},
    **{code: "AU" for code in AU_STATES},
}


def _codes(part: str) -> list[str]:
    """Upper-case words in a part, dots dropped: "D.C." -> "DC"."""
    return [w.replace(".", "") for w in part.split() if w.isupper()]


def _country_in(parts: list[str]) -> str:
    """A country the location names without doubt, or ""."""
    names = _country_names()
    for part in parts:
        name = _clean(part)
        if name in names:
            return names[name]
        # A code that is also a state or province is left to _ambiguous_code.
        if len(part) == 2 and part.isupper() and part not in REGION_COUNTRY:
            found = country_code(part)
            if found:
                return found
    return ""


def _ambiguous_code(parts: list[str]) -> str:
    """The first code in the location that is a state or province and a
    country too ("Berlin, DE", "St. John's, NL"), or ""."""
    for part in parts:
        for code in _codes(part):
            if code in REGION_COUNTRY and country_code(code):
                return code
    return ""


def _ref_km(point, zone: str) -> float:
    ref = _zone_points().get(zone)
    return _km(point, ref) if ref else math.inf


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
        codes = _codes(part)
        if text_country in ("", "US"):
            if name in us_names:
                return us_names[name]
            if name.replace(" ", "") == "dc":  # "D.C.", "d.c."
                return US_STATES["DC"][1]
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
    return _resolve(location, country, point)[0]


def _country(parts: list[str], coords=None, hint: str = "") -> tuple[str, bool]:
    """The country the location's text puts the place in, or "", plus
    whether that was a guess: a code that is a region and a country, read
    by the US-first convention because nothing else said which."""
    text_country = _country_in(parts)
    code = "" if text_country else _ambiguous_code(parts)
    if not code:
        return text_country, False
    # "IN": Indiana or India? A country from elsewhere (Precleared's is
    # stronger evidence than a code) settles it, even when it is neither,
    # then the coordinates; otherwise a US state is read as the state
    # and a province or Australian state code as the country.
    home = REGION_COUNTRY[code]
    if hint:
        return hint, False
    if coords:
        as_region = _region(parts, home, "")
        as_country = _country_zone(code, coords)
        return (home if as_region and _ref_km(coords, as_region)
                <= _ref_km(coords, as_country) else code), False
    return (home if home == "US" else code), True


def country_for(location: str = "", point: str = "") -> tuple[str, bool]:
    """The country a location puts a recipient in, and whether it is certain.

    Certain: a country named in the text, a US state, Canadian province or
    Australian state (by name or an unambiguous code), or a code that is a
    region and a country too settled by the cached coordinates. Not
    certain: that code read by convention alone (the reading comes back,
    marked so), a state reading the coordinates contradict, and the
    coordinates by themselves, which can put a place over a border. ""
    when nothing names one.
    """
    parts = _parts(location)
    coords = _point(point)
    country, guessed = _country(parts, coords)
    if guessed:
        return country, False
    ambiguous = "" if _country_in(parts) else _ambiguous_code(parts)
    if ambiguous and coords:
        region_zone = _region(parts, REGION_COUNTRY[ambiguous], "")
        country_zone = _country_zone(ambiguous, coords)
        if min(_ref_km(coords, region_zone), _ref_km(coords, country_zone)) > MAX_REGION_KM:
            return "", False
    zone = _region(parts, country, "")
    if zone and coords and _ref_km(coords, zone) > MAX_REGION_KM:
        # The state was a misreading ("Perth, WA" in Australia): see _resolve.
        zone = ""
        if country == "US":
            country = ""
    if zone:
        country = country or zone_country(zone)
    return country, bool(country)


def _resolve(location: str = "", country: str = "", point: str = "") -> tuple[str, bool]:
    """zone_for, plus whether the answer was a guess: a code that is a
    region and a country, read by the US-first convention because nothing
    else said which."""
    parts = _parts(location)
    coords = _point(point)
    hint = country_code(country)
    text_country, guessed = _country(parts, coords, hint)
    zone = _region(parts, text_country, hint)
    if zone and coords and _km(coords, _zone_points().get(zone, coords)) > MAX_REGION_KM:
        # The coordinates put it on another continent ("Perth, WA" in
        # Australia), so the state was a misreading.
        zone = ""
        if text_country == "US":
            text_country = ""
    if zone:
        return zone, guessed
    named = text_country or hint
    if named:
        # A country's main zone (no state, no coordinates) is a guess too.
        zone = _country_zone(named, coords)
        return zone, guessed or (bool(zone) and _main_zone_only(named, coords))
    if coords:
        return _nearest(coords, max_km=MAX_NEAREST_KM), False
    return "", False


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


def icp_country(config) -> str:
    """The one country every ``icp.geography`` entry is certainly in, or "".

    Precleared's default when ``prospect_country`` is unset: the ICP is
    where discovery looked. A default only, so Precleared's own finding
    still wins."""
    icp = getattr(config, "icp", None)
    coords = getattr(icp, "geo_coordinates", None) or {}
    found = set()
    for place in getattr(icp, "geography", None) or []:
        country, certain = country_for(place, coords.get(place, ""))
        if not certain:
            return ""
        found.add(country)
    return found.pop() if len(found) == 1 else ""


async def _place(state, prospect) -> tuple[str, str]:
    """The company's location and the "lat,lon" discovery cached for it."""
    location = ""
    if getattr(prospect, "company_id", ""):
        company = await state.get_company(prospect.company_id)
        location = (getattr(company, "location", "") or "") if company else ""
    point = ""
    if location.strip():
        cached = await state.get_setting("geo:" + location.strip().lower())
        point = "" if not cached or cached == "-" else cached
    return location, point


async def recipient_country(state, prospect) -> str:
    """The recipient's country when Harvey is sure of it, for Precleared's
    ``recipient_country``; "" otherwise, since a guess sent as fact would
    override Precleared's own evidence. See ``country_for``."""
    if prospect is None:
        return ""
    location, point = await _place(state, prospect)
    country, certain = country_for(location, point)
    if certain:
        return country
    # A stored zone may come from approximate coordinates or stale location
    # data. Without provenance, it must not override Precleared's evidence.
    return ""


async def recipient_zone(state, prospect, config, country: str = "") -> str:
    """This recipient's zone, worked out once and stored on the prospect.

    ``country`` is the recipient's country from Precleared, when a check
    returned one. Returns "" when nothing settles it."""
    if prospect is None:
        return ""
    if getattr(prospect, "timezone", ""):
        return prospect.timezone
    location, point = await _place(state, prospect)
    zone, guessed = _resolve(location, country_code(country), point)
    if zone and not guessed:
        prospect.timezone = zone
        await state.set_prospect_timezone(prospect.id, zone)
        return zone
    # Not stored while a country found later (Precleared's) could still
    # change it: a zone only the configured prospect_country settles, or a
    # code that is a state and a country ("Berlin, DE") read by convention.
    fallback = _prospect_country(config)
    if fallback and not country_code(country):
        return _resolve(location, fallback, point)[0] or zone
    return zone
