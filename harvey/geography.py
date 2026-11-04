"""Geography priority: which ``icp.geography`` entry a company is in.

``icp.geography`` is an ordered list, and its order is the priority: the
first entry is where the user most wants to sell, the next is the next
best, and a company in none of them still counts, only last. This is the
tiered-account model account-based sales uses (Tier 1, 2, 3): every account
stays on the list, and the scarce thing (drafts, sends) goes to the top
tier first.

An entry matches a company when:

- it has ``icp.geo_coordinates`` with a radius ("lat,lng,radius_km") and
  the company's cached coordinates are inside that circle;
- its place name is one of the company location's parts ("Seattle, WA"
  matches "Seattle, WA, USA" and "Seattle"), unless both name a country and
  the countries differ;
- it names a country alone ("United States", "Canada", "GB") and the
  company's location puts it in that country (see ``send_window.country_for``).

Anything else ("Western Europe", "EMEA") matches only by name. A company
whose location says nothing matches no entry, so it ranks with the rest.
"""

from __future__ import annotations

from harvey.send_window import _clean, _country_in, _km, _parts, _place, _point, country_for


def _radius(spec: str) -> tuple[tuple[float, float], float] | None:
    """(centre, radius_km) from a "lat,lng,radius_km" setting, or None."""
    centre = _point(spec)
    try:
        radius = float(str(spec or "").split(",")[2])
    except (IndexError, ValueError):
        return None
    return (centre, radius) if centre and radius > 0 else None


def _entry_country(entry: str) -> str:
    """The country an entry names when it is only a country, else ""."""
    parts = _parts(entry)
    return _country_in(parts) if len(parts) == 1 else ""


def matches(entry: str, location: str, point: str = "", coords: dict | None = None) -> bool:
    """True when a company at ``location`` (cached ``point``) is in ``entry``."""
    loc_parts = [_clean(p) for p in _parts(location)]
    if not loc_parts and not point:
        return False
    circle = _radius((coords or {}).get(entry, ""))
    here = _point(point)
    if circle and here and _km(here, circle[0]) <= circle[1]:
        return True
    country = _entry_country(entry)
    if country:
        return country_for(location, point)[0] == country
    entry_parts = _parts(entry)
    if not entry_parts:
        return False
    # _parts is last-first, so the place name is the last element.
    if _clean(entry_parts[-1]) not in loc_parts:
        return False
    theirs, ours = country_for(location, point)[0], country_for(entry, "")[0]
    return not (theirs and ours and theirs != ours)


def tier(location: str, config, point: str = "") -> tuple[int | None, str]:
    """(index into icp.geography, entry) for the first entry the company is
    in, or (None, "") when it is in none or nothing is configured."""
    icp = getattr(config, "icp", None)
    entries = list(getattr(icp, "geography", None) or [])
    coords = getattr(icp, "geo_coordinates", None) or {}
    for i, entry in enumerate(entries):
        if matches(entry, location, point, coords):
            return i, entry
    return None, ""


def rank(location: str, config, point: str = "") -> int:
    """A sort key: the tier index, or one past the last entry for a company
    in none, so "no match" sorts after every match and never drops out."""
    index, _ = tier(location, config, point)
    if index is not None:
        return index
    return len(getattr(getattr(config, "icp", None), "geography", None) or [])


async def prospect_rank(state, prospect, config) -> int:
    """``rank`` for a prospect, from its company's location and the
    coordinates discovery cached for it."""
    location, point = await _place(state, prospect)
    return rank(location, config, point)
