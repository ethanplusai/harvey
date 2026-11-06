"""What profiling and discovery learned about a company, as fact lines for
the Writer.

The Profile and Discover stages record findings as observations (no online
booking, ads running, Google Maps position, stale content, ...). The
dashboard shows them on the company card, but until now the Writer never
saw them: it only got the company's description, its tech stack and hiring
signals. So a first email could not open with "your site has no way to
request an estimate" even when Sellbird knew exactly that.

``website_facts`` turns the newest observation per signal (per search phrase
for rank signals) into plain fact lines. Booleans are stated both ways
("has online booking" as much as "no online booking") so the Writer never
reads a missing line as a finding. A failed read is "nothing known", never a
reason not to write.
"""

from __future__ import annotations

RANK_CODES = ("MAPS_RANK", "IN_LOCAL_PACK", "SERP_RANK")
MAX_RANK_LINES = 3


def _bool_line(code: str, on: bool) -> str | None:
    lines = {
        "NO_ONLINE_BOOKING": (
            "no online booking or scheduling found on their homepage: visitors "
            "can only phone or use a contact form to ask for a time or an estimate",
            "has online booking or scheduling on their homepage",
        ),
        "RUNNING_GOOGLE_ADS": ("running Google Ads (an ads tag is on their homepage)",
                               "not running Google Ads"),
        "RUNNING_META_ADS": ("running Meta/Facebook ads (a Meta pixel is on their homepage)",
                             "not running Meta/Facebook ads"),
        "UNCLAIMED_LISTING": ("their Google Business listing is unclaimed", None),
    }
    pair = lines.get(code)
    if pair is None:
        return None
    return pair[0] if on else pair[1]


def _rank_line(code: str, rank: int, phrase: str) -> str:
    phrase = f"'{phrase}'" if phrase else "their main search"
    if code == "MAPS_RANK":
        where = "below the three businesses Google shows first" if rank > 3 else "in the top three"
        return (f"Google Maps position: #{rank} for {phrase}, searched from the city "
                f"centre ({where})")
    if code == "IN_LOCAL_PACK":
        return f"holds slot {rank} of Google's local 3-pack for {phrase}"
    return f"Google search position: #{rank} for {phrase}, searched from the city centre"


def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def website_facts(state, company_id: str) -> list[str]:
    """Fact lines ("- Their website: ...", "- Google: ...") for one company, newest finding first
    per signal. Empty when nothing is known or the read fails."""
    if not company_id or state is None:
        return []
    lookup = getattr(state, "get_observations", None)
    if lookup is None:
        return []
    try:
        rows = await lookup(company_id=company_id)   # newest first
    except Exception:
        return []

    newest: dict[str, dict] = {}
    ranks: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row.get("prospect_id"):
            continue    # a fact about a person, not the company's site
        code = row.get("signal_code") or ""
        if code in RANK_CODES:
            ranks.setdefault((code, row.get("value_text") or ""), row)
        else:
            newest.setdefault(code, row)

    site: list[str] = []      # read on their own website
    google: list[str] = []    # from their Google listing and searches
    for code in ("NO_ONLINE_BOOKING", "RUNNING_GOOGLE_ADS", "RUNNING_META_ADS",
                 "UNCLAIMED_LISTING"):
        row = newest.get(code)
        value = _num(row.get("value_num")) if row else None
        if value is None:
            continue
        line = _bool_line(code, value >= 1)
        if line:
            (google if code == "UNCLAIMED_LISTING" else site).append(line)

    rank_lines = []
    for (code, phrase), row in ranks.items():
        rank = _num(row.get("value_num"))
        if rank is None or rank < 1:
            continue    # 0: that search no longer finds them
        rank_lines.append((int(rank), _rank_line(code, int(rank), phrase)))
    google.extend(line for _, line in sorted(rank_lines)[:MAX_RANK_LINES])

    stale = newest.get("BLOG_STALE")
    if stale:
        days = _num(stale.get("value_num"))
        text = (stale.get("value_text") or "").strip()
        if days is not None and text:
            site.append(f"{text} on their site (about {int(days // 30)} months ago, "
                         "from their sitemap)")

    rating = _num((newest.get("REVIEW_RATING") or {}).get("value_num"))
    count = _num((newest.get("REVIEW_COUNT") or {}).get("value_num"))
    if rating is not None and count is not None:
        google.append(f"Google rating {rating:g} from {int(count)} reviews")

    agency = (newest.get("INCUMBENT_AGENCY") or {}).get("value_text")
    if agency:
        site.append(f"site credited to a web agency: {agency}")

    return ([f"- Their website: {line}" for line in site]
            + [f"- Google: {line}" for line in google])
