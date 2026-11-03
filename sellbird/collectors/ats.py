"""Open roles, read from the job-board feed a company's ATS publishes.

Cost: $0, one request. Greenhouse, Lever, Ashby and Workable each publish a
company's open roles as public, unauthenticated JSON — the same data their
hosted job boards render. That beats reading a careers page: most careers
pages load their roles with JavaScript from exactly these feeds, so the HTML
we fetch has none of them, and the feed gives the department, location and
posting date as well as the title.

Which board a company uses comes from links and embeds on its OWN site
(``boards.greenhouse.io/<token>``, ``jobs.lever.co/<company>``, an embed
script). Never by guessing tokens: a guessed token that happens to exist is
somebody else's board, and the roles would be attributed to the wrong company.
"""

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import httpx

logger = logging.getLogger("harvey.collectors.ats")

# A board bigger than this is a large enterprise's, not a prospect worth one
# more request. Lever's feed carries full descriptions: ~6 MB for 300 roles.
MAX_FEED_BYTES = 8 * 1024 * 1024

ATS_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever", "lever_eu": "Lever",
              "ashby": "Ashby", "workable": "Workable"}

# Escaped slashes ("jobs.ashbyhq.com\/acme") show up in inline JSON.
_S = r"\\?/"
_TOKEN = r"([A-Za-z0-9][A-Za-z0-9_.%-]*)"
# (ats, pattern). The token is group 1 except where noted.
_BOARD_RES = (
    ("greenhouse", re.compile(
        rf"(?:job-)?boards\.greenhouse\.io{_S}embed{_S}job_(?:board|app)(?:{_S}js)?\?"
        r"(?:[^\"'\s<>]*?&(?:amp;)?)?for=([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(rf"boards-api\.greenhouse\.io{_S}v1{_S}boards{_S}([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(rf"(?<![\w.@-])(?:job-)?boards\.greenhouse\.io{_S}([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(rf"(?<![\w.@-])jobs\.lever\.co{_S}{_TOKEN}", re.I)),
    ("lever", re.compile(rf"(?<![\w.@-])api\.lever\.co{_S}v0{_S}postings{_S}{_TOKEN}", re.I)),
    ("lever_eu", re.compile(rf"(?<![\w.@-])jobs\.eu\.lever\.co{_S}{_TOKEN}", re.I)),
    ("lever_eu", re.compile(rf"(?<![\w.@-])api\.eu\.lever\.co{_S}v0{_S}postings{_S}{_TOKEN}", re.I)),
    ("ashby", re.compile(rf"(?<![\w.@-])jobs\.ashbyhq\.com{_S}{_TOKEN}", re.I)),
    ("ashby", re.compile(rf"api\.ashbyhq\.com{_S}posting-api{_S}job-board{_S}{_TOKEN}", re.I)),
    ("workable", re.compile(rf"apply\.workable\.com{_S}api{_S}v1{_S}widget{_S}accounts{_S}([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(rf"(?<![\w.@-])apply\.workable\.com{_S}([A-Za-z0-9_-]+)", re.I)),
    ("workable", re.compile(r"(?<![\w.@-])([a-z0-9][a-z0-9-]*)\.workable\.com(?![\w-])", re.I)),
)
# Path segments and subdomains that are the ATS's own pages, not a company.
_NOT_TOKENS = frozenset("""
embed api j v0 v1 jobs job job_board posting-api www apply app help resources
careers static assets cdn widget accounts blog login signin
""".split())
# Script and asset names on the ATS hosts ("boards.greenhouse.io/embed.js").
_ASSET = re.compile(r"\.(js|mjs|css|json|png|jpe?g|svg|gif|ico|webp|woff2?|map)$", re.I)
# A page linking this many boards, none of them named like the site, is a
# staffing agency's or a VC's portfolio page: none of them is the company's.
MANY_BOARDS = 3


@dataclass(frozen=True)
class Board:
    """One company's job board on one ATS."""

    ats: str        # greenhouse | lever | lever_eu | ashby | workable
    token: str      # the board's name in that ATS's URLs

    @property
    def label(self) -> str:
        return ATS_LABELS.get(self.ats, self.ats)

    @property
    def page_url(self) -> str:
        """The public board a person would open."""
        t = quote(self.token, safe="")
        return {
            "greenhouse": f"https://boards.greenhouse.io/{t}",
            "lever": f"https://jobs.lever.co/{t}",
            "lever_eu": f"https://jobs.eu.lever.co/{t}",
            "ashby": f"https://jobs.ashbyhq.com/{t}",
            "workable": f"https://apply.workable.com/{t}/",
        }[self.ats]


@dataclass
class Posting:
    """One open role, as the board lists it."""

    title: str
    department: str = ""
    location: str = ""
    posted: str = ""     # YYYY-MM-DD when the feed gives one
    url: str = ""

    def summary(self) -> str:
        """"Head of Growth · Marketing · New York, NY · posted 2026-10-01"."""
        parts = [self.title, self.department, self.location]
        if self.posted:
            parts.append(f"posted {self.posted}")
        return " · ".join(p for p in parts if p)[:200]


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def detect_board(html: str, domain: str = "") -> Board | None:
    """The job board this page links to or embeds, or None.

    A page can mention several (a customer story linking to a customer's
    board): the one named like the company's domain wins, then the one
    linked most often.
    """
    if not html:
        return None
    found: Counter = Counter()
    first: dict[Board, int] = {}
    for ats, pattern in _BOARD_RES:
        for m in pattern.finditer(html):
            token = unquote(m.group(1)).strip(".").strip()
            if not token or token.lower() in _NOT_TOKENS or _ASSET.search(token):
                continue
            board = Board(ats, token)
            found[board] += 1
            first.setdefault(board, m.start())
    if not found:
        return None
    name = _alnum((domain or "").split(".")[0] if domain else "")

    def named_like_them(board: Board) -> bool:
        token = _alnum(board.token)
        return len(name) >= 3 and len(token) >= 3 and (name in token or token in name)

    best = max(found, key=lambda b: (named_like_them(b), found[b], -first[b]))
    if not named_like_them(best) and len(found) >= MANY_BOARDS:
        return None
    return best


def feed_url(board: Board) -> str:
    """The documented public JSON feed for a board."""
    t = quote(board.token, safe="")
    return {
        # /departments rather than /jobs: the same roles, with the department
        # each sits in, in one request.
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{t}/departments",
        "lever": f"https://api.lever.co/v0/postings/{t}?mode=json",
        "lever_eu": f"https://api.eu.lever.co/v0/postings/{t}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{t}",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{t}",
    }[board.ats]


# ── parsing: every field is optional, every shape is checked ──

def _s(value) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _day(value) -> str:
    """YYYY-MM-DD from an ISO timestamp, a date, or epoch milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = _s(value)
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else ""


def _greenhouse(payload) -> list[Posting]:
    out, seen = [], set()
    departments = payload.get("departments") if isinstance(payload, dict) else None
    for dept in departments if isinstance(departments, list) else []:
        if not isinstance(dept, dict) or not isinstance(dept.get("jobs"), list):
            continue
        name = _s(dept.get("name"))
        if name.lower() == "no department":
            name = ""
        for job in dept["jobs"]:
            if not isinstance(job, dict) or not _s(job.get("title")):
                continue
            key = job.get("id") or job.get("absolute_url") or job.get("title")
            if key in seen:
                continue
            seen.add(key)
            loc = job.get("location")
            out.append(Posting(
                title=_s(job["title"]), department=name,
                location=_s(loc.get("name")) if isinstance(loc, dict) else "",
                posted=_day(job.get("first_published")) or _day(job.get("updated_at")),
                url=_s(job.get("absolute_url")),
            ))
    return out


def _lever(payload) -> list[Posting]:
    out = []
    for job in payload if isinstance(payload, list) else []:
        if not isinstance(job, dict) or not _s(job.get("text")):
            continue
        cats = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        out.append(Posting(
            title=_s(job["text"]),
            department=_s(cats.get("department")) or _s(cats.get("team")),
            location=_s(cats.get("location")),
            posted=_day(job.get("createdAt")),
            url=_s(job.get("hostedUrl")),
        ))
    return out


def _ashby(payload) -> list[Posting]:
    out = []
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or not _s(job.get("title")) or job.get("isListed") is False:
            continue
        out.append(Posting(
            title=_s(job["title"]), department=_s(job.get("department")),
            location=_s(job.get("location")), posted=_day(job.get("publishedAt")),
            url=_s(job.get("jobUrl")),
        ))
    return out


def _workable(payload) -> list[Posting]:
    out = []
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or not _s(job.get("title")):
            continue
        place = ", ".join(p for p in (_s(job.get(k)) for k in ("city", "state", "country")) if p)
        if not place and job.get("telecommuting"):
            place = "Remote"
        out.append(Posting(
            title=_s(job["title"]), department=_s(job.get("department")), location=place,
            posted=_day(job.get("published_on")) or _day(job.get("created_at")),
            url=_s(job.get("url")) or _s(job.get("shortlink")),
        ))
    return out


_PARSERS = {"greenhouse": _greenhouse, "lever": _lever, "lever_eu": _lever,
            "ashby": _ashby, "workable": _workable}


def parse_feed(ats: str, payload) -> list[Posting]:
    """The open roles in a feed. Anything unexpected is skipped, never raised."""
    parser = _PARSERS.get(ats)
    return parser(payload) if parser else []


async def read_board(client: httpx.AsyncClient, board: Board) -> list[Posting] | None:
    """Read one board's feed: one request, size-capped. None when the board
    doesn't answer with JSON (gone, renamed, or never existed)."""
    url = feed_url(board)
    body = bytearray()
    try:
        async with client.stream("GET", url, headers={"Accept": "application/json"}) as resp:
            if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
                logger.debug(f"ats: {url} → {resp.status_code}")
                return None
            if int(resp.headers.get("content-length") or 0) > MAX_FEED_BYTES:
                logger.info(f"ats: {board.label} board {board.token} too large, skipped")
                return None
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_FEED_BYTES:
                    logger.info(f"ats: {board.label} board {board.token} too large, skipped")
                    return None
    except (httpx.HTTPError, ValueError) as e:
        logger.debug(f"ats: fetch failed {url}: {e}")
        return None
    try:
        return parse_feed(board.ats, json.loads(bytes(body)))
    except (ValueError, RecursionError):
        # RecursionError: pathologically nested JSON, which json.loads
        # recurses into. A board that can't be read is no board.
        return None


# ── which roles count ──

_FILLER_WORDS = frozenset({"of", "the", "and", "for", "a", "an", "&", "to"})
# Spellings of the same role, folded to one before comparing words.
_EQUIVALENT = tuple((re.compile(p, re.I), r) for p, r in (
    (r"\bvice[\s-]+president\b", "vp"),
    (r"\bchief\s+executive\s+officer\b", "ceo"),
    (r"\bchief\s+marketing\s+officer\b", "cmo"),
    (r"\bchief\s+technology\s+officer\b", "cto"),
    (r"\bchief\s+technical\s+officer\b", "cto"),
    (r"\bchief\s+financial\s+officer\b", "cfo"),
    (r"\bchief\s+operating\s+officer\b", "coo"),
    (r"\bchief\s+revenue\s+officer\b", "cro"),
    (r"\bchief\s+product\s+officer\b", "cpo"),
    (r"\bsr\b\.?", "senior"),
    (r"\bjr\b\.?", "junior"),
    (r"\bfront[\s-]+end\b", "frontend"),
    (r"\bback[\s-]+end\b", "backend"),
    (r"\bfull[\s-]+stack\b", "fullstack"),
))
# "Executive Assistant to the CEO" is not a CEO role: only the words before
# "to (the)" name the role being hired.
_REPORTS_TO = re.compile(r"\s+to\s+(?:the\s+|our\s+)?\S.*$", re.I)


def _words(text: str) -> set[str]:
    for pattern, replacement in _EQUIVALENT:
        text = pattern.sub(replacement, text)
    return set(re.findall(r"[a-z0-9+#]+", text.lower()))


def role_keywords(icp) -> list[str]:
    """Roles that mean a company is in-market: ``icp.hiring_signals``, or
    ``icp.titles`` when none are set. Lowercased."""
    if icp is None:
        return []
    keywords = [k.strip().lower() for k in (getattr(icp, "hiring_signals", None) or [])
                if isinstance(k, str) and k.strip()]
    if keywords:
        return keywords
    return [t.strip().lower() for t in (getattr(icp, "titles", None) or [])
            if isinstance(t, str) and t.strip()]


def matching_roles(postings: list[Posting], keywords: list[str]) -> list[Posting]:
    """Postings whose title holds every word of some keyword, in any order:
    "VP Marketing" matches "VP of Marketing", "cto" doesn't match "Director"."""
    wanted = [w for w in ((_words(k) - _FILLER_WORDS) for k in keywords) if w]
    return [p for p in postings
            if any(w <= _words(_REPORTS_TO.sub("", p.title)) for w in wanted)]
