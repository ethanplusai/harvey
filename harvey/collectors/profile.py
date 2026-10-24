"""PROFILE collector — what a business is, from three HTTP requests.

Cost: $0. No browser, no model. Homepage + robots.txt + sitemap.xml (+ the
team and careers pages when present, and the company's public job-board feed
when its site links one), then regex over HTML we already hold.

This is the highest value-per-effort stage in the pipeline: it produces the
signals that actually make an email specific — who their incumbent agency is,
whether they're spending on ads today, what's missing from their site.
"""

import asyncio
import logging
from html import unescape as html_unescape
import random
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import httpx

from harvey.public_http import public_http_client

from harvey.collectors.ats import detect_board, matching_roles, read_board, role_keywords

from harvey.collectors.discover import (
    name_from_domain, name_matches_domain, site_name_from_html,
)

logger = logging.getLogger("harvey.collectors.profile")

TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=8.0)
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# ── Agency detection ──────────────────────────────────────────────────
# Links that appear in footers but are never the site's agency. Matched as
# substrings against the link host.
BENIGN_HOSTS = (
    # social
    "facebook.", "instagram.", "twitter.", "x.com", "linkedin.", "youtube.",
    "tiktok.", "pinterest.", "yelp.", "nextdoor.", "threads.",
    # maps / profiles
    "google.com/maps", "g.page", "maps.app.goo.gl", "goo.gl", "wa.me",
    "bing.com/maps", "apple.com/maps",
    # financing / payments / reviews
    "greensky", "wellsfargo", "synchrony", "affirm", "klarna", "paypal",
    "stripe.com", "bbb.org", "angi.com", "homeadvisor", "houzz.",
    "trustpilot", "birdeye", "podium.com",
    # accessibility / compliance widgets
    "accessibe", "userway", "audioeye", "termly", "iubenda", "onetrust",
    # infra / CDNs / platforms
    "cloudflare", "godaddy", "wordpress.org", "wix.com", "squarespace.com",
    "shopify.com", "webflow.com", "duda.co", "gtranslate", "jquery",
    "gstatic", "googleapis", "cloudfront", "akamai", "w3.org", "schema.org",
    # industry bodies / certs
    "gaf.com", "owenscorning", "certainteed", "nrca.net", "iko.com",
    "energystar.gov", "osha.gov", "nahb.org",
)

# Credit wording → confidence. Only >= 0.75 is recorded: a wrong incumbent in
# an email is worse than saying nothing at all.
STRONG_CREDIT = re.compile(
    r"\b(powered|website|site|web\s*design|designed|developed|built|created|"
    r"marketing)\s+(by|&)\b", re.I)
DESCRIPTIVE_CREDIT = re.compile(
    r"\b(web\s*design|website\s*design|digital\s*marketing|seo|marketing\s*"
    r"agency|web\s*development)\b", re.I)
RECORD_FLOOR = 0.75

# ── Tech / platform fingerprints (name -> regexes over raw HTML) ──
TECH_FINGERPRINTS: dict[str, tuple[str, ...]] = {
    "WordPress": (r"wp-content/", r"wp-includes/"),
    "Shopify": (r"cdn\.shopify\.com", r"myshopify\.com"),
    "Wix": (r"static\.parastorage\.com",),
    "Squarespace": (r"static1\.squarespace\.com",),
    "Webflow": (r"assets\.website-files\.com", r"data-wf-domain"),
    "Duda": (r"irp\.cdn-website\.com", r"d1\.awsstatic"),
    "GoDaddy Website Builder": (r"img1\.wsimg\.com",),
    "HubSpot": (r"js\.hs-scripts\.com", r"js\.hsforms\.net"),
    "Google Analytics": (r"googletagmanager\.com/gtag", r"google-analytics\.com"),
    "Google Tag Manager": (r"googletagmanager\.com/gtm\.js",),
    "Intercom": (r"widget\.intercom\.io",),
    "Drift": (r"js\.driftt\.com",),
    "Podium": (r"connect\.podium\.com",),
    "Birdeye": (r"birdeye\.com",),
    "Calendly": (r"assets\.calendly\.com",),
    "ServiceTitan": (r"servicetitan",),
    "Jobber": (r"getjobber\.com",),
    "Housecall Pro": (r"housecallpro",),
    "CallRail": (r"cdn\.callrail\.com",),
    "Stripe": (r"js\.stripe\.com",),
}

GOOGLE_ADS = (r"googleadservices\.com", r"gtag/js\?id=AW-", r"google_conversion",
              r"googletagmanager\.com/gtag/js\?id=AW-")
META_PIXEL = (r"connect\.facebook\.net/[^\"']*fbevents\.js", r"fbq\s*\(\s*['\"]init")

BOOKING_HINTS = (r"calendly\.com", r"acuityscheduling", r"schedulicity",
                 r"squareup\.com/appointments", r"setmore", r"book(ing)?[-_]?now",
                 r"schedule[-_]?(an?[-_])?(appointment|estimate|consultation)",
                 r"housecallpro", r"getjobber\.com", r"servicetitan")

AI_CRAWLERS = ("gptbot", "google-extended", "ccbot", "anthropic-ai", "claudebot",
               "perplexitybot", "bytespider")

TEAM_PATHS = ("/team", "/about", "/about-us", "/our-team", "/staff",
              "/leadership", "/meet-the-team", "/company/team")
CAREERS_PATHS = ("/careers", "/jobs", "/join-us", "/employment", "/hiring",
                 "/about/careers", "/careers/")
# A homepage link to the careers page beats guessing paths: careers.acme.com
# and /company/jobs are as common as /careers. Whole path segments and whole
# anchors only: "/scheduling/hiring-marketplaces" is a product page.
_CAREERS_HOST = re.compile(r"^(careers?|jobs|join)\.", re.I)
_CAREERS_SEGMENTS = frozenset("""careers career jobs join join-us joinus work-with-us
open-roles open-positions hiring""".split())
_CAREERS_ANCHOR = re.compile(r"^(careers?|jobs|join us|join (our|the) team|we['’]?re hiring"
                             r"|open (roles|positions)|work with us)$", re.I)
# Roles recorded per company from a board. The first few say it; twenty rows
# of "Account Executive, <city>" don't say more.
MAX_BOARD_ROLES = 5
CONTACT_PATHS = ("/contact", "/contact-us", "/get-a-quote", "/free-estimate",
                 "/request-estimate", "/quote")

# Words that are never part of a person's name. Without this, headings like
# "Request Service", "Baker History" and "Commercial Services" pass a
# Firstname-Lastname regex and become contacts — the single most common
# failure mode when scraping team pages.
NON_NAME_WORDS = frozenset("""
service services repair repairs roofing roof roofs contractor contracting
commercial residential industrial storm damage restoration installation
history about contact quote quotes estimate estimates free request get call
learn more read view our their your team staff careers jobs home page site
company companies inc llc ltd corp group solutions systems partners
gallery projects reviews testimonials financing warranty insurance claims
maintenance inspection replacement gutters siding windows doors metal shingle
shingles tile flat emergency 24 hour hours today now new best top quality
privacy policy terms conditions sitemap blog news events why choose us
areas served locations location city county state north south east west
español espanol menu skip navigation search close open toggle
hoa hoas condo condos association associations board boards community
communities management property properties owners residents customer care
scope alignment implementation consistency communication process driven
focused structured
""".split())

# Tokens that are words, not names, whatever the vertical: abstract nouns
# ("Scope Alignment", "Board Communication") and shouted acronyms ("HOA").
_NOUN_SUFFIX = re.compile(r"(tion|tions|sion|sions|ness)$")


def _is_name_token(token: str) -> bool:
    for part in token.split("-"):
        bare = part.strip(".,'")
        low = bare.lower()
        if low in NON_NAME_WORDS or _NOUN_SUFFIX.search(low):
            return False
        if len(bare) >= 3 and bare.isupper():
            return False
    return True


# Block-level tags separate a name, its title, and the bio that follows; the
# first text chunk after the name heading is the title, nothing further.
_BLOCK_BREAK = re.compile(r"(?i)<(?:/?(?:p|div|li|ul|ol|section|article|td|tr)\b[^>]*|br\s*/?)>")
_CONNECTORS = frozenset("of and the for & to in at".split())


def _title_chunks(raw_after: str) -> list[str]:
    raw_after = re.sub(r"<[^>]*$", "", raw_after)   # a tag cut off by the window
    chunks = []
    for piece in _BLOCK_BREAK.split(raw_after):
        text = html_unescape(re.sub(r"(?s)<[^>]+>", " ", piece))
        text = re.sub(r"\s+", " ", text).strip(" ,.|-–—·")
        if text:
            chunks.append(text)
    return chunks


def _reads_like_title(text: str) -> bool:
    """A job title is short and mostly capitalised; a sentence is neither.

    "Desiring consistent oversight and a trusted management partner" contains
    the role word "partner" but is marketing copy, not a title.
    """
    words = text.split()
    if not words or len(words) > 10:
        return False
    lower = [w for w in words[1:] if w[:1].islower() and w.lower() not in _CONNECTORS]
    return len(lower) <= 2 and title_looks_like_role(text)

DECISION_MAKER_TITLES = (
    "owner", "founder", "co-founder", "president", "ceo", "principal",
    "partner", "general manager", "operations manager", "practice manager",
    "office manager", "marketing director", "marketing manager",
    "director of marketing", "vice president", "vp ", "managing director",
    "chief", "director",
)


ROLE_HINT = re.compile(
    r"\b(owner|founder|president|ceo|cfo|coo|cto|principal|partner|manager|"
    r"director|supervisor|superintendent|foreman|estimator|specialist|"
    r"consultant|coordinator|representative|rep|sales|marketing|operations|"
    r"office|project|account|service|technician|installer|inspector|"
    r"vice president|vp|executive|lead|head of|chief)\b", re.I)


def title_looks_like_role(text: str) -> bool:
    """True when the text following a name reads like a job title.

    A name with no role nearby is usually a nav link or a page heading, not
    a person — requiring a role is the cheapest high-precision filter.
    """
    return bool(ROLE_HINT.search(text[:160]))


def _strip_code(html: str) -> str:
    """Remove script/style/comments BEFORE any text regex.

    Skipping this is how you end up with contacts named 'return value instan'
    whose title is CEO — minified JS read as prose.
    """
    html = re.sub(r"(?is)<script\b.*?</script>", " ", html)
    html = re.sub(r"(?is)<style\b.*?</style>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    return html


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", _strip_code(html)))


# ── YC_BACKED: what a company says about itself on its own site ──
# Never read from ycombinator.com or YC's other sites, whose terms forbid
# scraping and collecting founders' contacts. What counts is the company's
# own claim: a reference to its own YC page, or a sentence about itself
# ("we", "our", its name) or a badge-short line ("YC W24", "Backed by Y
# Combinator"). Lines about customers and audiences ("trusted by YC-backed
# startups", "for YC founders") and YC programs (Startup School, Demo Day,
# applying) are not.
YC_OWNED_DOMAINS = ("ycombinator.com", "workatastartup.com", "startupschool.org")
_YC = r"(?:YC|Y[\s-]?Combinator)"
_YC_LINK_RE = re.compile(r"ycombinator\.com\\?/companies\\?/([a-z0-9][\w-]*)", re.I)
YC_ABOUT_PATHS = ("/about", "/about-us", "/company", "/our-story")
_YC_SEASONS = {"w": "W", "winter": "W", "s": "S", "summer": "S",
               "f": "F", "fall": "F", "x": "X", "spring": "X"}
_SEASON_WORD = r"(Winter|Summer|Fall|Spring)"
# A batch right next to YC: "YC W24", "(YC S23)", "Y Combinator's Summer 2023
# batch", "Y Combinator alumni (W19)", "the Summer 2023 batch of Y Combinator".
_YC_BATCH_RES = (
    re.compile(rf"\b{_YC}(?:['’]s)?\s*(?:(?:alumni|alums?|company|startup)\s*)?[\(\[]?\s*"
               r"(W|S|F|X)\s*'?(\d{2})\b(?![\w-]*\s*(?:Ultra|Plus|Pro|FE|Max)\b)", re.I),
    re.compile(rf"\b{_YC}(?:['’]s)?\s+{_SEASON_WORD}\s+(?:batch\s+)?(\d{{4}})\b", re.I),
    re.compile(rf"\b{_SEASON_WORD}\s+(\d{{4}})\s+batch\s+(?:of|at)\s+{_YC}\b", re.I),
    re.compile(rf"\b{_YC}\s+(?:in|during)\s+(?:the\s+)?{_SEASON_WORD}\s+(?:of\s+)?(\d{{4}})\b", re.I),
    re.compile(rf"\b{_YC}\s+demo day\s*[\(\[]\s*(W|S|F|X)\s*'?(\d{{2}})\b", re.I),
)
# What a claim looks like. Anchored at the start of a sentence, so the
# company is the one being described, not a customer, a team member or an
# audience ("trusted by YC-backed startups", "we sponsored the YC dinner").
_BATCH = r"(?:W|S|F|X)\s*'?\d{2}"
_BADGE_RE = re.compile(  # a whole line that is a badge: backing words or a batch, not a bare "YC"
    rf"^\(?\s*(?:(?:backed|funded|supported)\s+by\s+{_YC}(?:\s*[\(\[]?\s*{_BATCH}\s*[\)\]]?)?"
    rf"|{_YC}[\s-]+(?:backed|funded)(?:\s*[\(\[]?\s*{_BATCH}\s*[\)\]]?)?"
    rf"|{_YC}\s*[\(\[]?\s*{_BATCH}\s*[\)\]]?)\s*\)?\.?$", re.I)
_FILLER = r"(?:\s+(?:proudly|also|now|officially|recently|originally|first|both|all|so|were|was|are|is|been|have|has|had|very))*"
_CLAIM_BODY = (
    rf"{_FILLER}\s+(?:backed by|funded by|supported by|part of|a member of|members of|graduated from|"
    rf"alumni of|alums? of|went through|joined|hatched (?:in|at)|incubated (?:in|at|by)|accepted into|"
    rf"participated in|launched at|in)\s+(?:the\s+)?{_YC}\b"
    rf"|{_FILLER}\s+in\s+the\s+{_SEASON_WORD}\s+\d{{4}}\s+batch\s+of\s+{_YC}\b"
    rf"|{_FILLER}\s+(?:a|an)\s+{_YC}(?:\s+{_BATCH})?[\s-]+(?:(?:backed|funded)\s+)?"
    rf"(?:company|startup|platform|product|business|team|tool)\b"
    rf"|{_FILLER}\s+{_YC}[\s-]+(?:backed|funded|alumni|alums?|alumnus)\b"
    rf"|\s*[\(\[]\s*{_YC}\s*{_BATCH}\s*[\)\]]"
)
# "in YC" only after a form of be ("we were in YC"), not "believe in YC".
_IN_NEEDS_BE = re.compile(rf"(?<!\bwere)(?<!\bwas)(?<!\bare)(?<!\bis)(?<!\bbeen)\s+in\s+(?:the\s+)?{_YC}\b", re.I)
_SELF_SUBJECT = (r"(?:we|we're|we are|we were|we've been|proud(?:ly)? to be|excited to be|honou?red to be"
                 r"|the company|our company)")
_FOUNDED_PREFIX = r"(?:founded in \d{4},?\s+)?"
# "<subject> ... and is backed by YC", but not "...companies that are backed by YC".
_LATER_BACKING = (rf"[^.]{{0,80}}?(?<!\bthat)(?<!\bwhich)(?<!\bwho)\s+(?:is|are|was|were|has been|have been)"
                  rf"\s+(?:proudly\s+)?(?:backed|funded|supported)\s+by\s+{_YC}\b")
_INVESTORS_RE = re.compile(rf"^(?:our\s+)?investors?\s*(?:include|including|are|:)[^.]{{0,80}}\b{_YC}\b", re.I)
_LEADING_BACKING_RE = re.compile(rf"^(?:backed|funded|supported)\s+by\s+{_YC}\b", re.I)
# A YC program right after the mention, with no batch: not backing.
_YC_PROGRAM_RE = re.compile(
    rf"\b{_YC}(?:['’]s)?\s+(?:startup school|demo day|application|interview|events?|podcast|blog|news|jobs|deals?|perks?)\b",
    re.I)
_ATTR_TEXT_RE = re.compile(r"""\b(?:alt|title|aria-label)=["']([^"']{0,200})["']""", re.I)
_BLOCK_TAG_RE = re.compile(r"(?i)</?(?:p|div|h[1-6]|li|ul|ol|br|section|footer|header|nav|article|td|tr|table|blockquote|figcaption|dt|dd)\b[^>]*>")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\s*[|•·]\s*|\n")
_PREFIXES = re.compile(r"^(?:get|try|use|join)(?=\w{4})")
_SUFFIXES = re.compile(r"(?<=\w{4})hq$")
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def _lines(html: str) -> list[str]:
    """Readable sentences, with block boundaries kept (a badge isn't run into
    the next paragraph) and entities decoded ("Y Combinator&#x27;s W20")."""
    text = _BLOCK_TAG_RE.sub("\n", _strip_code(html))
    text = html_unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text += "\n" + "\n".join(html_unescape(a) for a in _ATTR_TEXT_RE.findall(html))
    out = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        out.extend(p.strip() for p in _SENTENCE_RE.split(line) if p.strip())
    return out


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _domain_name(domain: str) -> str:
    """The registrable name: app.acme.com and acme.co.uk are both "acme"."""
    parts = [p for p in domain.lower().strip(".").split(".") if p]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def _is_their_yc_page(slug: str, domain: str) -> bool:
    """A YC page link is theirs when its name matches their domain's: a
    customer logo can link to that customer's YC page."""
    name, label = _alnum(slug), _alnum(_domain_name(domain)) if domain else ""
    if not name or not label:
        return False
    if name == label:
        return True
    # getacme.com / acmehq.com and the slug "acme", nothing looser.
    core = lambda t: _SUFFIXES.sub("", _PREFIXES.sub("", t))
    return min(len(core(name)), len(core(label))) >= 4 and core(name) == core(label)


def _yc_batch(match) -> str:
    season, year = _YC_SEASONS[match.group(1).lower()], match.group(2)[-2:]
    return f"{season}{year}" if 5 <= int(year) <= 40 else ""


def yc_backed(html: str, domain: str = "") -> tuple[str, float] | None:
    """(batch like "W24" or "", confidence) when the page says the company
    itself is YC-backed, else None."""
    if not html:
        return None
    link = any(_is_their_yc_page(m.group(1), domain) for m in _YC_LINK_RE.finditer(html))
    name = _domain_name(domain) if domain else ""
    # The name as written on the page may have spaces or hyphens ("Acme Corp"
    # for acmecorp.com): any separators between its letters.
    spelled = r"[\s.-]?".join(re.escape(ch) for ch in _alnum(name)) if len(_alnum(name)) >= 3 else ""
    subject = rf"(?:{_SELF_SUBJECT}" + (rf"|{spelled}\b" if spelled else "") + r")"
    claim_re = re.compile(rf"^{_FOUNDED_PREFIX}{subject},?(?:{_CLAIM_BODY}|{_LATER_BACKING})", re.I)
    batch, claimed = "", False
    for sentence in _lines(html):
        if not re.search(rf"\b{_YC}\b", sentence, re.I):
            continue
        found = next((m for r in _YC_BATCH_RES for m in [r.search(sentence)] if m), None)
        match = claim_re.search(sentence)
        if match and _IN_NEEDS_BE.search(match.group(0)) and not re.search(
                r"\bin\s+the\s+\w+\s+\d{4}\s+batch|hatched in|incubated in|participated in", match.group(0), re.I):
            match = None
        ok = bool(_BADGE_RE.match(sentence) or match or _INVESTORS_RE.search(sentence)
                  or _LEADING_BACKING_RE.search(sentence))
        if ok and not found and _YC_PROGRAM_RE.search(sentence):
            ok = False      # Startup School, Demo Day, a YC blog: a program, not backing
        if ok:
            claimed = True
            if found and not batch:
                batch = _yc_batch(found)
    if link:
        return batch, 0.95
    if claimed:
        return batch, 0.9 if batch else 0.85
    return None


class ProfileCollector:
    """Fetches a business's public pages and emits observations.

    Only signals the user CONFIRMED are collected; everything else is skipped
    even if the data is sitting right there in the HTML.
    """

    def __init__(self, state, confirmed: set[str] | None = None, timeout: float = 12.0,
                 role_keywords: list[str] | None = None):
        self.state = state
        self.confirmed = confirmed if confirmed is not None else set()
        self.timeout = timeout
        # icp.hiring_signals (or icp.titles): which open roles are worth a row.
        self.role_keywords = list(role_keywords or [])
        self._client: httpx.AsyncClient | None = None
        self.site_names: dict[str, str] = {}

    def wants(self, code: str) -> bool:
        return code in self.confirmed

    async def __aenter__(self):
        self._client = public_http_client(
            timeout=TIMEOUT,
            headers={"User-Agent": random.choice(USER_AGENTS),
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    async def _get(self, url: str) -> str:
        try:
            resp = await self._client.get(url)
        except (httpx.HTTPError, ValueError) as e:
            logger.debug(f"fetch failed {url}: {e}")
            return ""
        if resp.status_code != 200:
            return ""
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            return ""
        return resp.text

    async def _resolve_site(self, domain: str) -> tuple[str, str]:
        """Return (working base url, homepage html), or ("", "") if none work.

        Tries https/http and bare/www variants — a plain https guess marks
        plenty of live, real small-business sites "unreachable" simply
        because they never got a TLS cert, or only answer on www.
        """
        for base in (
            f"https://{domain}", f"https://www.{domain}",
            f"http://{domain}", f"http://www.{domain}",
        ):
            html = await self._get(base)
            if html:
                return base, html
        return "", ""

    async def _first_present(self, site: str, paths) -> tuple[str, str]:
        """Return (url, html) for the first path that returns content."""
        for path in paths:
            url = f"{site}{path}"
            html = await self._get(url)
            if html and len(html) > 500:
                return url, html
        return "", ""

    async def _careers_page(self, site: str, home: str, domain: str) -> tuple[str, str]:
        """(url, html) of the careers page: the homepage's own link to it
        (same domain or a subdomain), else the first common path that exists."""
        for link in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
                                _strip_code(home)):
            url = urljoin(site + "/", link.group(1).strip())
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            if parsed.scheme not in ("http", "https") or not (
                    host == domain or host.endswith("." + domain)):
                continue
            anchor = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", link.group(2))).strip()
            segments = {seg.lower() for seg in parsed.path.split("/") if seg}
            if (segments or host != domain) and (
                    _CAREERS_HOST.match(host) or segments & _CAREERS_SEGMENTS
                    or _CAREERS_ANCHOR.match(html_unescape(anchor))):
                html = await self._get(url)
                if html and len(html) > 500:
                    return url, html
                break
        return await self._first_present(site, CAREERS_PATHS)

    async def _hiring(self, site: str, home: str, domain: str, add) -> None:
        curl, chtml = "", ""
        board = detect_board(home, domain)
        if board is None:
            curl, chtml = await self._careers_page(site, home, domain)
            board = detect_board(chtml, domain)
        postings = await read_board(self._client, board) if board else None
        if postings is not None:
            add("OPEN_ROLES", num=len(postings), text=board.label, url=board.page_url)
            keywords = getattr(self, "role_keywords", None) or []
            matched = matching_roles(postings, keywords)[:MAX_BOARD_ROLES]
            for post in matched:
                add("HIRING_ROLE", text=post.summary(), url=post.url or board.page_url)
            if not matched:
                # The board answered and lists nothing relevant: say so, so
                # last month's (now closed) roles stop being the current view.
                add("HIRING_ROLE", num=0, url=board.page_url)
            return
        if board and not chtml:
            curl, chtml = await self._careers_page(site, home, domain)
        if chtml and self.wants("HIRING_ROLE"):
            roles = []
            for m in re.finditer(
                r"(?is)<(h[2-5]|a|li)[^>]*>\s*([^<]{6,70})\s*</\1>", _strip_code(chtml)
            ):
                t = re.sub(r"\s+", " ", m.group(2)).strip()
                if re.search(r"\b(hiring|apply|position|opening|wanted|"
                             r"technician|installer|sales|manager|foreman|"
                             r"estimator|crew|representative)\b", t, re.I):
                    if t.lower() not in [r.lower() for r in roles]:
                        roles.append(t)
                if len(roles) >= 3:
                    break
            for r in roles:
                add("HIRING_ROLE", text=r, url=curl)

    # ── individual detectors ──

    def detect_agency(self, html: str, domain: str) -> tuple[str, float, str] | None:
        """Find the agency credited in the footer. (name, confidence, url)."""
        cleaned = _strip_code(html)
        m = re.search(r"(?is)<footer\b.*?</footer>", cleaned)
        region = m.group(0) if m else cleaned[-12000:]

        best = None
        for link in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', region):
            href, anchor = link.group(1), re.sub(r"(?s)<[^>]+>", " ", link.group(2))
            anchor = re.sub(r"\s+", " ", anchor).strip()
            if not href.startswith("http"):
                continue
            host = (urlparse(href).netloc or "").lower().replace("www.", "")
            if not host or host.endswith(domain) or domain.endswith(host):
                continue
            if any(b in href.lower() or b in host for b in BENIGN_HOSTS):
                continue
            if not anchor or len(anchor) > 80:
                continue

            # Context = the ~120 chars before the link, where credits live.
            start = max(0, link.start() - 120)
            context = re.sub(r"(?s)<[^>]+>", " ", region[start:link.start()])
            blob = f"{context} {anchor}"

            if STRONG_CREDIT.search(blob):
                conf = 0.9
            elif DESCRIPTIVE_CREDIT.search(anchor):
                conf = 0.75
            else:
                conf = 0.35  # a bare link — do not record
            if conf >= RECORD_FLOOR and (best is None or conf > best[1]):
                best = (anchor[:80], conf, href)
        return best

    def detect_tech(self, html: str) -> list[str]:
        found = []
        for name, patterns in TECH_FINGERPRINTS.items():
            if any(re.search(p, html, re.I) for p in patterns):
                found.append(name)
        return sorted(found)

    @staticmethod
    def _any(html: str, patterns) -> bool:
        return any(re.search(p, html, re.I) for p in patterns)

    def detect_people(self, html: str) -> list[dict]:
        """Names + titles from a team page. Conservative on purpose."""
        text_html = _strip_code(html)
        people, seen = [], set()
        # Name in a heading, title in the nearby text.
        # Tag names match any case; the NAME must not — under a blanket (?i)
        # "Customer care" passes the Firstname Lastname capitalisation test.
        for m in re.finditer(
            r"(?s)<((?i:h[1-5]|strong|b))[^>]*>\s*([A-Z][a-z'\-]+(?:\s+[A-Z]\.?)?\s+"
            r"[A-Z][a-zA-Z'\-]+)\s*</(?i:\1)>(?=(.{0,220}))", text_html
        ):
            # NOTE the lookahead above: capturing the trailing context normally
            # would CONSUME it, so finditer would skip every person whose entry
            # fell inside the previous person's 220-char window.
            name = re.sub(r"\s+", " ", m.group(2)).strip()
            # Bound the context at the NEXT heading, in raw HTML, before
            # stripping tags — otherwise one member's role bleeds into the
            # next member's entry.
            raw_after = m.group(3)
            nxt = re.search(r"(?i)<(h[1-5]|strong|b)\b", raw_after)
            if nxt:
                raw_after = raw_after[: nxt.start()]
            if name.lower() in seen or len(name) > 45:
                continue
            # Reject obvious non-names
            if re.search(r"\d|@|\bthe\b|\bour\b|\bwe\b", name, re.I):
                continue
            # Reject headings that merely LOOK like names. Every token must be
            # plausibly a name part; one business word disqualifies the whole
            # match ("Request Service", "Baker History", "HOA Boards").
            if not all(_is_name_token(t) for t in name.split()):
                continue
            # A real contact needs a title that reads like a role; a bare
            # heading with no role text is almost always navigation, and a
            # heading followed by a sentence is a feature list.
            title = next((c for c in _title_chunks(raw_after)[:2]
                          if _reads_like_title(c)), "")
            if not title:
                continue
            seen.add(name.lower())
            parts = name.split()
            people.append({
                "first_name": parts[0], "last_name": " ".join(parts[1:]),
                "title": title[:80],
                "is_decision_maker": any(t in title.lower() for t in DECISION_MAKER_TITLES),
            })
            if len(people) >= 12:
                break
        return people

    # ── the stage itself ──

    async def profile_company(self, company_id: str, domain: str) -> list[dict]:
        """Collect every confirmed profile signal for one business."""
        obs: list[dict] = []
        domain = domain.lower().replace("www.", "").strip("/")
        if any(domain == d or domain.endswith("." + d) for d in YC_OWNED_DOMAINS):
            # YC's own sites forbid scraping; they're never a prospect anyway.
            return obs

        def add(code, *, num=None, text="", conf=1.0, url=""):
            if not self.wants(code):
                return
            obs.append({
                "signal_code": code, "company_id": company_id, "collector": "profile",
                "value_num": num, "value_text": text, "confidence": conf,
                "evidence_url": url or site,
            })

        site, home = await self._resolve_site(domain)
        if not home:
            # A failure IS an observation — silent failures look like clean results.
            logger.info(f"profile: {domain} unreachable")
            return obs
        self.site_names[company_id] = site_name_from_html(home, domain)

        # Ads (they are spending TODAY — the strongest budget signal here)
        add("RUNNING_GOOGLE_ADS", num=1 if self._any(home, GOOGLE_ADS) else 0)
        add("RUNNING_META_ADS", num=1 if self._any(home, META_PIXEL) else 0)

        # Platform + tooling
        tech = self.detect_tech(home)
        if tech:
            add("TECH_STACK", text=", ".join(tech))

        # Structured data gap
        has_schema = bool(re.search(r'application/ld\+json', home, re.I)
                          and re.search(r'"@type"', home))
        add("NO_SCHEMA_MARKUP", num=0 if has_schema else 1)

        # Booking gap
        add("NO_ONLINE_BOOKING", num=0 if self._any(home, BOOKING_HINTS) else 1)

        # YC-backed, as the company says on its own site: the homepage, or
        # its About page (where most say it, if anywhere).
        if self.wants("YC_BACKED"):
            yc, where = yc_backed(home, domain), site
            if yc is None:
                about_url, about = await self._first_present(site, YC_ABOUT_PATHS)
                if about:
                    yc, where = yc_backed(about, domain), about_url
            # Most YC companies never say it on these pages, so silence
            # doesn't erase a YC-preset search hit (collector "discover").
            # It does correct an earlier profile "yes", so a wrong tag or a
            # dropped claim doesn't last forever.
            searched = False
            if yc is None and getattr(self, "state", None) is not None:
                try:
                    searched = await self.state.yc_search_hit(company_id)
                except Exception:
                    searched = False
            if yc is not None or not searched:
                add("YC_BACKED", num=1 if yc else 0, text=yc[0] if yc else "",
                    conf=yc[1] if yc else 1.0, url=where if yc else site)

        # Incumbent agency — the compounding signal
        agency = self.detect_agency(home, domain)
        if agency:
            add("INCUMBENT_AGENCY", text=agency[0], conf=agency[1], url=agency[2])

        # robots.txt → AI crawler policy
        if self.wants("BLOCKS_AI_CRAWLERS"):
            robots = (await self._get(f"{site}/robots.txt")).lower()
            blocked = any(
                re.search(rf"user-agent:\s*{re.escape(bot)}[\s\S]{{0,200}}?disallow:\s*/",
                          robots)
                for bot in AI_CRAWLERS
            )
            add("BLOCKS_AI_CRAWLERS", num=1 if blocked else 0,
                url=f"{site}/robots.txt")

        # sitemap.xml → size + content freshness
        if self.wants("SITE_PAGE_COUNT") or self.wants("BLOG_STALE"):
            sm = await self._get(f"{site}/sitemap.xml")
            if sm:
                locs = re.findall(r"<loc>\s*([^<]+)\s*</loc>", sm)
                if self.wants("SITE_PAGE_COUNT") and locs:
                    add("SITE_PAGE_COUNT", num=len(locs), url=f"{site}/sitemap.xml")
                if self.wants("BLOG_STALE"):
                    dates = re.findall(r"<lastmod>\s*(\d{4}-\d{2}-\d{2})", sm)
                    if dates:
                        newest = max(dates)
                        try:
                            age = (datetime.now(timezone.utc).date()
                                   - datetime.strptime(newest, "%Y-%m-%d").date()).days
                            add("BLOG_STALE", num=age, text=f"newest content {newest}",
                                url=f"{site}/sitemap.xml")
                        except ValueError:
                            pass

        # contact form — reaches the business without needing an email at all
        if self.wants("CONTACT_FORM_URL"):
            if re.search(r"(?is)<form\b", _strip_code(home)):
                add("CONTACT_FORM_URL", text=site, url=site)
            else:
                curl, chtml = await self._first_present(site, CONTACT_PATHS)
                if chtml and re.search(r"(?is)<form\b", _strip_code(chtml)):
                    add("CONTACT_FORM_URL", text=curl, url=curl)

        # open roles → hiring intent. The job-board feed when their site
        # links one (titles, departments and dates for every role), else
        # whatever the careers page's own HTML lists. Any failure here costs
        # only these two signals, never the rest of the profile.
        if self.wants("HIRING_ROLE") or self.wants("OPEN_ROLES"):
            try:
                await self._hiring(site, home, domain, add)
            except Exception as e:
                logger.info(f"profile: {domain} open roles unread: {type(e).__name__}: {e}")

        # team page → people
        if self.wants("CONTACT_FOUND") or self.wants("DECISION_MAKER_TITLE"):
            turl, thtml = await self._first_present(site, TEAM_PATHS)
            if thtml:
                for person in self.detect_people(thtml):
                    label = f"{person['first_name']} {person['last_name']}"
                    add("CONTACT_FOUND",
                        text=f"{label} — {person['title']}" if person["title"] else label,
                        url=turl)
                    if person["is_decision_maker"]:
                        add("DECISION_MAKER_TITLE",
                            text=f"{label} — {person['title']}", url=turl)

        return obs

    async def _improve_name(self, company: dict, domain: str):
        """Replace a name the domain doesn't back up ("Denver, CO | HOA
        Management Firm") or one merely derived from the domain ("Grow With
        Us") with the one the homepage gives itself. Only a distinctive brand match
        is kept; generic industry and navigation text does not establish identity."""
        better = self.site_names.pop(company["id"], "")
        current = company.get("name") or ""
        fallback = current == name_from_domain(domain)
        if not better or better == current or (
                name_matches_domain(current, domain) and not fallback):
            return
        try:
            await self.state.rename_company(company["id"], better)
            logger.info(f"profile: {domain} named '{better}' (was '{current}')")
        except Exception as e:
            logger.debug(f"rename_company failed for {domain}: {e}")

    async def _mark_checked(self, company_id: str):
        try:
            await self.state.mark_company_profiled(company_id)
        except Exception as e:
            logger.debug(f"mark_company_profiled failed for {company_id}: {e}")

    async def run(self, companies: list[dict], run_id: str = "") -> int:
        """Profile many businesses, flushing observations after each one.

        Flush-per-item is deliberate: the observations are the asset, and a
        long run that dies must not take them with it.
        """
        total = 0
        for company in companies:
            domain = (company.get("domain") or "").strip()
            if not domain:
                continue
            try:
                obs = await self.profile_company(company["id"], domain)
            except Exception as e:
                logger.warning(f"profile failed for {domain}: {e}")
                await self._mark_checked(company["id"])
                continue
            # Marked regardless of outcome — an unreachable site still counts
            # as checked, or the same handful gets retried every cycle
            # forever instead of rotating through the rest of the backlog.
            await self._mark_checked(company["id"])
            await self._improve_name(company, domain)
            if obs:
                total += await self.state.add_observations(obs, run_id=run_id)
                logger.info(f"profile: {domain} → {len(obs)} observation(s)")
            await asyncio.sleep(random.uniform(0.4, 1.2))
        return total


async def profile_companies(state, companies: list[dict], config=None) -> tuple[int, str]:
    """Convenience entry point: opens a run, profiles, closes the run.

    ``config`` supplies the roles that count as hiring intent
    (``icp.hiring_signals``, else ``icp.titles``); without it a job board
    still gives OPEN_ROLES, but no role is singled out.
    """
    from harvey.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    confirmed = await state.confirmed_signal_codes()
    if not confirmed:
        logger.warning(
            "profile: no signals confirmed yet — confirm signals first "
            "(nothing is collected until you choose what matters)."
        )
        return 0, ""
    run_id = await state.start_run("profile", provider="http")
    try:
        keywords = role_keywords(getattr(config, "icp", None))
        async with ProfileCollector(state, confirmed, role_keywords=keywords) as collector:
            n = await collector.run(companies, run_id=run_id)
        await state.finish_run(run_id, records=n, cost_usd=0.0)
        return n, run_id
    except Exception as e:
        await state.finish_run(run_id, status="failed", error=str(e))
        raise

