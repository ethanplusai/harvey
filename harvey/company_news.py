"""What a company says about itself lately, on its own site.

Three free profile signals, read from pages Harvey already fetches or the one
page the homepage links to for them:

- COMPANY_NEWS: dated announcements on the company's own /press, /news,
  /newsroom or /blog page, each sorted into a kind of event: an acquisition,
  a leadership change, an expansion, a launch, a partnership, a certification
  or an award. Raises are FUNDING_ROUND's and are skipped here.
- COMPLIANCE_BADGES: security, privacy and quality standards the site
  mentions (SOC 2, ISO 27001, HIPAA, PCI DSS, ...).
- TOOLS_NAMED: business software the site names (QuickBooks, NetSuite,
  Salesforce, Guidewire, ...).

Each is what the site says, never a verdict: a mention of SOC 2 isn't proof
they hold it, and naming QuickBooks doesn't say they run it. Announcements
without a date are never stored, and the writer only treats one as news for
NEWS_RECENT_DAYS after its date, so "recent" is never a guess.
"""

import json
import re
from datetime import date, datetime
from html import unescape
from urllib.parse import urljoin, urlparse

NEWS_PATHS = ("/press", "/news", "/newsroom", "/blog")
# An announcement older than this isn't written to as news (about 4 months).
NEWS_RECENT_DAYS = 120
MAX_ITEMS = 3
MAX_HEADLINE = 160
# How far from a headline a date may sit in the page and still be its date.
DATE_WINDOW = 400

_CODE_RE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>|<!--.*?-->")
_JSONLD_RE = re.compile(
    r"(?is)<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>")
_HEADLINE_RE = re.compile(r"(?is)<(h[1-4]|a)\b([^>]*)>(.*?)</\1>")
_HREF_RE = re.compile(r"""\bhref\s*=\s*["']([^"'#]+)["']""", re.I)

_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december"
    .split(), 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MON = r"(january|february|march|april|may|june|july|august|september|october|november|december" \
       r"|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?"
_DATE_RES = (
    ("time", re.compile(r"""<time\b[^>]*\bdatetime\s*=\s*["'](\d{4})-(\d{2})-(\d{2})""", re.I)),
    ("iso", re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d])")),
    ("mdy", re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)),
    ("dmy", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON},?\s+(\d{{4}})\b", re.I)),
)

# (kind, pattern over the headline). First match wins, so the specific
# kinds come before the broad ones.
_KINDS = (
    ("acquisition", re.compile(
        r"\b(acquires?|acquired|acquisition|merges?\s+with|merger|to\s+be\s+acquired)\b", re.I)),
    ("leadership", re.compile(
        r"\b(appoints?|appointed|names?|named|hires?|hired|welcomes?|promotes?|promoted|"
        r"joins?\s+as|new)\b.{0,60}\b(ceo|cfo|cto|coo|cio|ciso|cmo|cro|chief|president|"
        r"vice\s+president|vp|head\s+of|managing\s+(director|partner)|board)\b", re.I)),
    ("certification", re.compile(
        r"\b(soc\s?2|iso\s?27001|iso\s?9001|hipaa|pci(\s?dss)?|hitrust|fedramp|cmmc|"
        r"certified|certification|accredited|accreditation)\b", re.I)),
    ("partnership", re.compile(
        r"\b(partners?\s+with|partnership|teams?\s+up|alliance|integrates?\s+with|"
        r"selected\s+by|selects|chooses)\b", re.I)),
    ("expansion", re.compile(
        r"\b(opens?|opened|expands?|expanded|expansion|enters|relocates?|"
        r"new\s+(office|offices|location|headquarters|hq|market|facility))\b", re.I)),
    ("launch", re.compile(
        r"\b(launch(es|ed)?|introduc(es|ed|ing)|unveil(s|ed)?|now\s+available|"
        r"general\s+availability|releases?|released)\b", re.I)),
    ("award", re.compile(
        r"\b(wins?|won|awards?|awarded|named\s+(to|one\s+of|a)|recogni[sz]ed|ranks?|ranked)\b", re.I)),
)
# A raise is FUNDING_ROUND's, which already reads these pages for one.
_FUNDING_RE = re.compile(
    r"\b(raises?|raised|funding|series\s+[a-e]\b|seed\s+round|investment\s+from)", re.I)

# Standards a site may mention. Order is the order they are reported in.
_STANDARDS = (
    ("SOC 2", re.compile(r"\bsoc\s?(2|ii)\b", re.I)),
    ("ISO 27001", re.compile(r"\biso(/iec)?\s?27001\b", re.I)),
    ("ISO 9001", re.compile(r"\biso\s?9001\b", re.I)),
    ("HIPAA", re.compile(r"\bhipaa\b", re.I)),
    ("HITRUST", re.compile(r"\bhitrust\b", re.I)),
    ("PCI DSS", re.compile(r"\bpci(\s?-?dss)?\b(?=[\s,.;:)\-]|$)", re.I)),
    ("GDPR", re.compile(r"\bgdpr\b", re.I)),
    ("FedRAMP", re.compile(r"\bfedramp\b", re.I)),
    ("CMMC", re.compile(r"\bcmmc\b", re.I)),
)

# Business software named in a site's text. Names that are also ordinary words
# are matched case-sensitively, or only in a longer product name ("Sage
# Intacct", never "sage advice").
_TOOLS = (
    ("QuickBooks", r"\bquick\s?books\b", False),
    ("Xero", r"\bXero\b", True),
    ("NetSuite", r"\bnet\s?suite\b", False),
    ("Sage Intacct", r"\bsage\s+intacct\b", False),
    ("FreshBooks", r"\bfreshbooks\b", False),
    ("Bill.com", r"\bbill\.com\b", False),
    ("Gusto", r"\bGusto\b", True),
    ("ADP", r"\bADP\b", True),
    ("Paychex", r"\bpaychex\b", False),
    ("Workday", r"\bWorkday\b", True),
    ("Salesforce", r"\bsalesforce\b", False),
    ("HubSpot", r"\bhubspot\b", False),
    ("Microsoft Dynamics", r"\b(microsoft\s+)?dynamics\s+(365|crm|ax|gp|nav)\b", False),
    ("SAP", r"\bSAP\b", True),
    ("ServiceNow", r"\bservicenow\b", False),
    ("Zendesk", r"\bzendesk\b", False),
    ("Guidewire", r"\bguidewire\b", False),
    ("Duck Creek", r"\bduck\s?creek\b", False),
    ("Applied Epic", r"\bapplied\s+epic\b", False),
    ("Vertafore", r"\bvertafore\b", False),
    ("Plaid", r"\bPlaid\b", True),
    ("Stripe", r"\bStripe\b", True),
    ("athenahealth", r"\bathena\s?health\b", False),
    ("Procore", r"\bprocore\b", False),
    ("Snowflake", r"\bSnowflake\b", True),
    ("Databricks", r"\bdatabricks\b", False),
    ("Jira", r"\bJira\b", True),
)
_TOOL_RES = tuple((name, re.compile(p, 0 if case else re.I)) for name, p, case in _TOOLS)

# Homepage links worth one more request for these signals.
TRUST_LINK_RE = re.compile(r"/(trust|security|compliance)(?:[/.\-?]|$)", re.I)
TOOLS_LINK_RE = re.compile(r"/(integrations?|partners|technology|tech-stack|apps)(?:[/.\-?]|$)", re.I)


def visible_text(html: str) -> str:
    """The page's text, with scripts, styles, comments and tags removed."""
    text = re.sub(r"<[^>]+>", " ", _CODE_RE.sub(" ", html or ""))
    return " ".join(unescape(text).split())


def _clean(text: str) -> str:
    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", text or "")).split())
    return text.replace("|", "/")[:MAX_HEADLINE].rstrip()


def _valid(y: int, m: int, d: int, today: date) -> date | None:
    try:
        when = date(y, m, d)
    except ValueError:
        return None
    return when if date(1995, 1, 1) <= when <= today else None


def _dates(html: str, today: date) -> list[tuple[int, date]]:
    """(position, date) for every date written in the page."""
    found = []
    for kind, pattern in _DATE_RES:
        for m in pattern.finditer(html):
            g = m.groups()
            if kind in ("time", "iso"):
                when = _valid(int(g[0]), int(g[1]), int(g[2]), today)
            elif kind == "mdy":
                when = _valid(int(g[2]), _MONTHS[g[0].lower().rstrip(".")], int(g[1]), today)
            else:
                when = _valid(int(g[2]), _MONTHS[g[1].lower().rstrip(".")], int(g[0]), today)
            if when:
                found.append((m.start(), when))
    return found


def kind_of(headline: str) -> str:
    """The kind of event a headline announces, or "" (also for a raise)."""
    if not headline or _FUNDING_RE.search(headline):
        return ""
    for kind, pattern in _KINDS:
        if pattern.search(headline):
            return kind
    return ""


def _jsonld_items(html: str, page_url: str, today: date) -> list[dict]:
    items = []
    for raw in _JSONLD_RE.findall(html or ""):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            headline, published = node.get("headline"), node.get("datePublished")
            if not (isinstance(headline, str) and isinstance(published, str)):
                continue
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", published)
            when = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)), today) if m else None
            url = node.get("url") if isinstance(node.get("url"), str) else ""
            if when:
                items.append({"date": when, "headline": _clean(headline),
                              "url": urljoin(page_url, url) if url else page_url})
    return items


def news_items(html: str, page_url: str, today: date | None = None) -> list[dict]:
    """Dated announcements on a news/press/blog page, newest first, each
    {"date", "kind", "headline", "url"}. Only items with a date and a kind
    of event; at most MAX_ITEMS."""
    today = today or date.today()
    found = _jsonld_items(html, page_url, today)
    page = _CODE_RE.sub(" ", html or "")
    dates = _dates(page, today)
    # Every headline-looking element, so each one's date is looked for only
    # between it and its neighbours: an undated item never takes the date of
    # the item next to it.
    heads = [(m, _clean(m.group(3))) for m in _HEADLINE_RE.finditer(page)]
    heads = [(m, h) for m, h in heads if 15 <= len(h) <= MAX_HEADLINE]

    def gap(pos, m):
        return 0 if m.start() <= pos < m.end() else min(abs(pos - m.start()), abs(pos - m.end()))

    # Each date belongs to the headline it is closest to (within DATE_WINDOW).
    owned: dict[int, list[tuple[int, date]]] = {}
    for pos, when in dates:
        best = min(range(len(heads)), key=lambda i: gap(pos, heads[i][0]), default=None)
        if best is not None and gap(pos, heads[best][0]) <= DATE_WINDOW:
            owned.setdefault(best, []).append((gap(pos, heads[best][0]), when))
    for i, (m, headline) in enumerate(heads):
        near = owned.get(i)
        if not near or not kind_of(headline):
            continue
        # A link: its own href, or the first link inside a heading.
        href = _HREF_RE.search(m.group(2) if m.group(1).lower() == "a" else m.group(3))
        url = urljoin(page_url, href.group(1)) if href else page_url
        found.append({"date": min(near)[1], "headline": headline, "url": url})
    out, seen = [], set()
    for item in sorted(found, key=lambda i: i["date"], reverse=True):
        kind = kind_of(item["headline"])
        key = item["headline"].lower()
        if not kind or key in seen:
            continue
        seen.add(key)
        out.append({**item, "kind": kind})
        if len(out) >= MAX_ITEMS:
            break
    return out


def news_text(items: list[dict]) -> str:
    """How COMPANY_NEWS is stored: "2026-03-14 · launch · Headline | ..."."""
    return " | ".join(f"{i['date'].isoformat()} · {i['kind']} · {i['headline']}" for i in items)


def parse_news_text(text: str) -> list[dict]:
    out = []
    for part in (text or "").split(" | "):
        bits = [b.strip() for b in part.split(" · ", 2)]
        if len(bits) != 3:
            continue
        try:
            when = datetime.strptime(bits[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        out.append({"date": when, "kind": bits[1], "headline": bits[2]})
    return out


def standards_mentioned(html: str) -> list[str]:
    text = visible_text(html)
    return [name for name, pattern in _STANDARDS if pattern.search(text)]


def tools_named(html: str) -> list[str]:
    text = visible_text(html)
    return [name for name, pattern in _TOOL_RES if pattern.search(text)]


def own_link(home: str, site: str, domain: str, pattern: re.Pattern) -> str:
    """The first link on the homepage, on the company's own domain, whose path
    matches ``pattern`` (a trust or integrations page), or ""."""
    for href in _HREF_RE.findall(home or ""):
        url = urljoin(site + "/", href.strip())
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme in ("http", "https") and (host == domain or host.endswith("." + domain)) \
                and pattern.search(parsed.path or ""):
            return url
    return ""


async def site_facts(state, company_id: str, today: date | None = None) -> list[str]:
    """Fact lines for the writer's prompt from the three signals ([] when
    there are none). News older than NEWS_RECENT_DAYS is left out, so it is
    never written to as news. A failed read is "nothing known"."""
    if not company_id or state is None or getattr(state, "get_observations", None) is None:
        return []
    try:
        rows = await state.get_observations(company_id=company_id, latest_only=True, limit=500)
    except Exception:
        return []
    latest = {r.get("signal_code"): r for r in rows}
    today = today or date.today()
    facts: list[str] = []

    row = latest.get("COMPANY_NEWS")
    if row and (row.get("value_num") or 0) >= 1:
        for item in parse_news_text(row.get("value_text") or ""):
            age = (today - item["date"]).days
            if 0 <= age <= NEWS_RECENT_DAYS:
                weeks = max(1, age // 7)
                article = "an" if item["kind"][:1] in "aeiou" else "a"
                facts.append(
                    f"- Announced on their own site on {item['date'].isoformat()} "
                    f"(about {weeks} week{'s' if weeks != 1 else ''} ago), {article} "
                    f"{item['kind']}: \"{item['headline']}\". Quote it only as their "
                    "announcement")
    row = latest.get("COMPLIANCE_BADGES")
    if row and (row.get("value_num") or 0) >= 1 and row.get("value_text"):
        facts.append(f"- Standards their site mentions: {row['value_text']} (a mention on "
                     "their site, not proof they hold them)")
    row = latest.get("TOOLS_NAMED")
    if row and (row.get("value_num") or 0) >= 1 and row.get("value_text"):
        facts.append(f"- Business software their site names: {row['value_text']} (named on "
                     "their site; it doesn't say they run it)")
    return facts
