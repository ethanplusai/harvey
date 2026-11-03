"""PEOPLE — who decides at a small business, read off its own website.

Cost: $0. Pure functions over HTML that Scout (and the profile collector)
already fetched: no browser, no model, no paid lookup.

A small business rarely has a tidy "team" page. The owner is in a <span>
with a <br> ("Dale Whitlock<br>Owner and CEO"), in a sentence ("Led today by
President Ilan Brooks"), in the homepage's schema.org ("founder": [...]), or
only implied ("the office of Dr. Maren Hollis Pryor", a surname on the
door of "Ashby & Kerr"). This module reads all of those, and is strict
about what is NOT a person: headings made of ordinary words ("Team of
Experts"), customers quoted in testimonials ("Co-founder and CEO at Bluefinch
Labs"), and blog authors in schema.org.

Each person comes back as a dict:
    first_name, last_name, title, owner (bool), rank (int),
    source ("card" | "schema" | "prose"), evidence (str), evidence_url (str)
"""

import html as html_lib
import json
import re
from urllib.parse import unquote, urljoin, urlparse

from harvey.collectors.profile import (
    NON_NAME_WORDS, ROLE_HINT, _NOUN_SUFFIX, _reads_like_title, _strip_code,
)

# ── what a name is ───────────────────────────────────────────────────────

# Ordinary words that headings, nav links and section titles are built from.
# One of these in a "name" means it isn't one ("Team of Experts", "Meet The
# Team", "Family Owned"). Kept apart from profile.NON_NAME_WORDS, which is
# about trade vocabulary; both apply.
COMMON_WORDS = frozenset("""
a an and or the of for to in on at by with from our your my we us you me it is are
team teams expert experts people person member members staff crew leadership leader
leaders meet welcome hello hi thanks thank family owned operated local trusted
professional professionals certified licensed insured quality best top great good
read more less view see all learn know about contact call book schedule now today
here there this that these those who what why how where when story mission vision
values culture history office offices hours open closed home page blog news events
press media careers jobs join apply portal login sign account cart shop store
founder founders owner owners president partners partner principal director
manager managers doctor doctors dentist dentists attorney attorneys lawyer lawyers
dr mr mrs ms miss sir madam associates associate secret sauce happy smile smiles
proud since years year experience care patient patients client clients customer
customers testimonial testimonials review reviews award awards
realty realtors consulting consultants capital ventures holdings enterprises industries
technologies technology media studio studios labs agency digital creative analytics
software sports medicine fitness salon spa dental medical clinic insurance financial
properties inc llc pllc ltd corp co
coach pastor father mother judge mayor senator governor captain sergeant sheriff officer
uncle aunt grandpa grandma mom dad memory memoriam late former
""".split())

# Particles that sit lower-case inside a surname ("Van de Wal", "de la Cruz").
_PARTICLES = frozenset("van von de der den del della di da la le du dos das st".split())
_HONORIFIC = re.compile(r"^(?:dr|mr|mrs|ms|miss|prof|rev|hon)\.?\s+", re.I)
# Credentials and suffixes that follow a name: "John Smith, DDS", "Calvin Marsh, Esq."
_CREDENTIAL = (r"(?:dds|dmd|md|do|dvm|od|phd|jd|esq|cpa|pe|ra|rn|np|pa|mba|ms|ma|"
               r"facs|fagd|ccc-slp|jr|sr|ii|iii|iv)")
_CREDENTIAL_TAIL = re.compile(rf"(?:\s*,?\s+{_CREDENTIAL}\.?)+\s*$", re.I)
_NAME_TOKEN = re.compile(r"^[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*\.?$|^[A-Z]\.$|^[A-Z]$")


def _word_ok(token: str) -> bool:
    low = token.strip(".,'’").lower()
    if not low or low in COMMON_WORDS or low in NON_NAME_WORDS:
        return False
    return not _NOUN_SUFFIX.search(low)


def split_person_name(text: str) -> tuple[str, str] | None:
    """(first, last) for a real-looking personal name, else None.

    Drops honorifics ("Dr."), middle initials ("Renata D. Ashby"),
    middle names ("Maren Hollis Pryor" -> last "Pryor") and trailing
    credentials ("John Smith DDS"); keeps lower-case surname particles
    ("Pieter Van de Wal").
    """
    text = html_lib.unescape(text or "").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip(" ,.-|–—")
    text = re.sub(r"(?i)^(?:meet|introducing)\s+", "", text)
    text = _HONORIFIC.sub("", text)
    text = _CREDENTIAL_TAIL.sub("", text).strip(" ,")
    tokens = text.split()
    if not 2 <= len(tokens) <= 5 or len(text) > 45:
        return None
    if any(ch.isdigit() for ch in text) or "@" in text:
        return None
    first = tokens[0]
    if not re.match(r"^[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*$", first) or not _word_ok(first):
        return None
    rest = tokens[1:]
    # Surname: any particles that start it, then capitalised words.
    start = len(rest) - 1
    while start > 0 and rest[start - 1].lower() in _PARTICLES:
        start -= 1
    surname = rest[start:]
    middles = rest[:start]
    for t in middles:
        if not (_NAME_TOKEN.match(t) and (len(t.strip(".")) == 1 or _word_ok(t))):
            return None
    last_word = surname[-1]
    if not re.match(r"^[A-Z][a-zA-Z]*(?:[-'’][A-Z]?[a-zA-Z]+)*$", last_word) or len(last_word) < 2:
        return None
    if last_word.isupper() and len(last_word) > 2:      # "RAY T", "HVAC"
        return None
    if not _word_ok(last_word):
        return None
    if first.isupper() or any(t.lower() not in _PARTICLES for t in surname[:-1]):
        return None
    return first, " ".join(surname)


def is_person_name(text: str) -> bool:
    return split_person_name(text) is not None


# ── titles ───────────────────────────────────────────────────────────────

# Titles that mean "the person who decides" at a small business. Any one of
# them satisfies an ICP that lists any other, so "Proprietor" is found by an
# ICP that says "Owner", and "Founder & CEO" by one that says "President".
_OWNER_FAMILY = ("owner", "co-owner", "founder", "co-founder", "cofounder", "president",
                 "ceo", "chief executive", "chief executive officer", "principal",
                 "proprietor", "managing partner", "founding partner", "managing member",
                 "owner/operator", "owner-operator")
_OWNER_TITLE = re.compile(
    r"\b(?:co-?owner|owner|co-?founder|founder|proprietor|ceo|chief executive(?: officer)?|"
    r"president|managing (?:partner|member)|founding (?:partner|attorney|dentist|doctor)|"
    r"principal(?! (?:designer|engineer|consultant|architect|associate|scientist|developer)))\b", re.I)
# Words that make an owner title about someone else ("Assistant to the CEO")
# or a rank below it ("Vice President", "VP").
_SUBORDINATE = re.compile(r"\b(?:vice[- ]president|vp|svp|evp|assistant(?: to)?|"
                          r"executive assistant|to the|deputy|associate|aide)\b", re.I)
_PROFESSIONAL = re.compile(
    r"\b(?:dds|dmd|md|dvm|od|esq|cpa|attorney|lawyer|dentist|orthodontist|"
    r"periodontist|endodontist|doctor|dr|physician|veterinarian|optometrist|"
    r"chiropractor|partner|counsel|architect)\b\.?", re.I)
_PARTNER_ONLY = re.compile(r"^(?:senior |founding |managing |name |equity )?partner\b", re.I)


# Someone who used to own or run it: "Former Owner", "Past President",
# "Retired President", "Founder (1952-2001)", "the late", "Emeritus".
_FORMER = re.compile(
    r"(?i)\b(?:former|formerly|past|retired|late|emerit(?:us|a)|deceased|in memory|memoriam|"
    r"ex-|one-time|onetime)\b|\b(?:1[89]|20)\d{2}\s*[-–—]\s*(?:1[89]|20)\d{2}\b")


def is_owner_title(title: str) -> bool:
    title = (title or "").strip()
    if not title or _SUBORDINATE.search(title) or _FORMER.search(title):
        return False
    return bool(_OWNER_TITLE.search(title) or _PARTNER_ONLY.match(title))


def _clean_for_target(title: str, target: str) -> str:
    """Remove the parts of a title that would make a target match by accident:
    "Vice President" for a "President" target, "to the CEO" for "CEO"."""
    low = f" {title.lower()} "
    if "vice" not in target and "vp" not in target.split():
        low = re.sub(r"\bvice[- ]president\b|\b[se]?vp\b", " ", low)
    if "assistant" not in target:
        low = re.sub(r"\b(?:executive )?assistant\b.*", " ", low)
    low = re.sub(r"\bto the\b.*", " ", low)
    return low


def title_matches(title: str, targets, owner: bool = False) -> bool:
    """Does a job title satisfy any ICP target title?

    Word-based, not substring: "Vice President" doesn't satisfy "President"
    (unless the ICP lists it), "Partnerships Manager" doesn't satisfy
    "Partner". Owner synonyms satisfy any owner-type target, and ``owner``
    (evidence beyond the title, like a surname on the door) does too.
    """
    title = (title or "").strip()
    targets = [t.strip().lower() for t in (targets or []) if t and t.strip()]
    if not targets or _FORMER.search(title):
        return False
    owner_targets = [t for t in targets if t in _OWNER_FAMILY or t == "partner"]
    if owner_targets and (owner or is_owner_title(title)):
        return True
    if not title:
        return False
    for t in targets:
        cleaned = _clean_for_target(title, t)
        if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w])", cleaned):
            return True
    return False


def owner_rank(title: str, owner: bool = False) -> int:
    """How surely this title is the decision maker (higher first)."""
    low = (title or "").lower()
    if is_owner_title(title):
        if re.search(r"\b(?:co-?owner|owner|proprietor)\b", low):
            return 100
        return 95
    if owner:
        return 90
    if re.search(r"\bpartner\b", low):
        return 80
    if re.search(r"\b(?:general manager|managing director)\b", low):
        return 60
    if re.search(r"\b(?:director|manager|head of|chief)\b", low) and not _SUBORDINATE.search(low):
        return 40
    return 10


# ── whose business is it ─────────────────────────────────────────────────

_GENERIC_BUSINESS_WORDS = frozenset("""
the and of a an inc llc pllc plc pc ltd co corp company group firm law legal office
offices practice dental dentistry family roofing roof roofers heating cooling hvac
air plumbing electric services service solutions marketing agency media digital
creative studio studios design partners associates construction contractors
contracting home homes repair center clinic health care analytics software labs
""".split())


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _domain_label(domain: str) -> str:
    host = (domain or "").lower().strip().removeprefix("www.")
    host = re.sub(r"^https?://", "", host).split("/")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def _business_blob(domain: str, site_name: str = "") -> str:
    return _domain_label(domain) + "|" + _alnum(site_name)


def names_business(text: str, domain: str, site_name: str = "") -> bool:
    """Does an organisation name refer to this business? ("Tallyway Analytics"
    is tallyway.test; "Bluefinch Labs" is not countwise.test.)"""
    blob = _business_blob(domain, site_name)
    words = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
             if len(w) >= 3 and w not in _GENERIC_BUSINESS_WORDS]
    if any(w in blob for w in words):
        return True
    squashed = _alnum(text)
    label = _domain_label(domain)
    return len(squashed) >= 4 and (squashed in blob or (len(label) >= 4 and label in squashed))


def surname_on_the_door(last_name: str, domain: str, site_name: str = "") -> bool:
    """The person's surname is in the business name: "Ashby & Kerr",
    hollisfamilydental.test. Owners name their practices after themselves."""
    blob = _business_blob(domain, site_name)
    for part in re.findall(r"[A-Za-z]+", last_name or ""):
        p = part.lower()
        if len(p) >= 4 and p not in _GENERIC_BUSINESS_WORDS and p in blob:
            return True
    return False


# An owner title that names another organisation: a quoted customer
# ("Founder at Quillnest", "Co-founder and CEO at Bluefinch Labs", "founder
# Signary") or a role outside the business ("President of the NC Chapter").
_TITLE_ORG = re.compile(
    r"(?i:\b(?:owner|founder|co-?founder|ceo|cto|coo|cmo|president|principal|partner|director)"
    r"\b(?:\s*(?:,|&|and)\s*\w+)*?\s*(?:,|\bat\b|\bof\b|@|\s))\s*"
    r"((?:the\s+)?[A-Z][\w&.'’-]*(?:\s+(?:[A-Z][\w&.'’-]*|&|and|of))*)")


def title_names_other_org(title: str, domain: str, site_name: str = "") -> bool:
    for m in _TITLE_ORG.finditer(title or ""):
        org = m.group(1).strip()
        if re.fullmatch(r"(?i)(?:and\s+)?(?:owner|founder|co-?founder|ceo|cto|coo|cmo|president|"
                        r"principal|partner|director|chief[\w\s]*|operations|executive officer)", org):
            continue
        if not names_business(org, domain, site_name):
            return True
    return False


# ── text of a page, as lines ─────────────────────────────────────────────

_QUOTED_CLASS = re.compile(r"""(?is)<(\w+)\b[^>]*\bclass\s*=\s*["'][^"']*\b(?:testimonials?|reviews?|tweets?|"""
                           r"""quotes?|blockquote|client-say|customer-say|endorsements?)\b[^"']*["'][^>]*>""")
_STATUS_LINK = re.compile(r"""(?is)<a\b[^>]*href\s*=\s*["'][^"']*(?:twitter|x)\.com/[^"']*/status/[^"']*["'][^>]*>.*?</a>""")


_VOID_TAGS = frozenset("img input br hr meta link source area col embed wbr track param".split())
MAX_QUOTE_CHARS = 8000


MAX_QUOTES = 200


def _close_of(html: str, start: int, tag: str) -> int:
    """Index just past the element opened at ``start`` (same-tag depth
    count), looking no further than MAX_QUOTE_CHARS ahead; ``start`` when it
    doesn't close within that window."""
    depth, pos = 0, start
    pattern = re.compile(rf"(?i)<(/?){re.escape(tag)}\b[^>]*?(/?)>")
    for m in pattern.finditer(html, start, min(len(html), start + MAX_QUOTE_CHARS)):
        if m.group(2):          # self-closing
            continue
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return m.end()
    return start


def drop_quoted(html: str) -> str:
    """Remove testimonials, reviews and embedded tweets: the people quoted
    there are customers ("Paula Grey, Product designer and founder"), never
    the business's own staff."""
    html = _STATUS_LINK.sub(" ", html or "")
    out, pos = [], 0
    for n, m in enumerate(_QUOTED_CLASS.finditer(html)):
        if n >= MAX_QUOTES:
            break
        if m.start() < pos or m.group(1).lower() in _VOID_TAGS:
            continue
        end = _close_of(html, m.start(), m.group(1))
        if end <= m.start():
            continue    # a whole section that merely contains reviews, not one quote
        out.append(html[pos:m.start()])
        pos = max(end, m.end())
    out.append(html[pos:])
    html = "".join(out)
    return re.sub(r"(?is)<blockquote\b(?:(?!</?blockquote).){0,8000}</blockquote>", " ", html)


_LINE_BREAK = re.compile(
    r"(?i)<(?:/?(?:p|div|li|ul|ol|h[1-6]|section|article|header|footer|nav|td|tr|table|"
    r"blockquote|figcaption|dt|dd|span|a|strong|b|em|label|button)\b[^>]*|br\s*/?)>")


def page_lines(html: str) -> list[str]:
    """Visible text split at element boundaries: a name and the title under
    it ("Dale Whitlock<br>Owner and CEO") land on separate lines."""
    text = _LINE_BREAK.sub("\n", _strip_code(html or ""))
    text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", text)).replace("\xa0", " ")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip(" |·•")
        if line:
            lines.append(line)
    return lines


def _sentences(html: str) -> list[str]:
    """Readable sentences (block boundaries kept, inline tags joined)."""
    text = re.sub(r"(?i)<(?:/?(?:p|div|li|h[1-6]|section|article|td|tr|blockquote|figcaption|dd|dt)\b[^>]*|br\s*/?)>",
                  "\n", _strip_code(html or ""))
    text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", text)).replace("\xa0", " ")
    out = []
    for block in text.split("\n"):
        block = re.sub(r"\s+", " ", block).strip()
        # "Dr. Maren", "Elliot T. Marwood": an abbreviation's full stop
        # is not the end of a sentence.
        block = re.sub(r"\b(Dr|Mr|Mrs|Ms|Prof|St|Jr|Sr|Inc|Co|Ltd|[A-Z])\. ", "\\1.\xa0", block)
        if block:
            out.extend(s.strip() for s in re.split(r"(?<=[.!?]) +(?=[A-Z])", block) if s.strip())
    return out


# ── people on cards and lists ────────────────────────────────────────────

# A line that is a quotation: a testimonial, so the next line is its author.
_QUOTE_LINE = re.compile(r"^\s*[\"“‘']|[\"”’]\s*$|[★⭐]")
_SEPARATED = re.compile(r"^(?P<name>[^,|–—\-]{4,45}?)\s*(?:,|\||–|—|\s-\s)\s*(?P<title>.{2,80})$")
# On one line after a comma, only an unmistakable job title counts: "Nate
# Calloway, proud advocate for the trades." is a tagline.
_STRONG_ROLE = re.compile(
    r"\b(?:owner|co-?owner|founder|co-?founder|president|ceo|cfo|coo|cto|cmo|cro|chief|principal|"
    r"partner|proprietor|director|manager|attorney|lawyer|dds|dmd|md|esq|cpa|dentist|"
    r"orthodontist|officer|head of|vp|vice president|superintendent|estimator|administrator)\b", re.I)


def _person(first, last, title, *, source, evidence, url="", owner=False, full_name=""):
    return {"first_name": first, "last_name": last, "title": title[:80].strip(" ,;:-–—|"),
            "full_name": full_name or f"{first} {last}",
            "owner": owner, "source": source, "evidence": evidence[:200],
            "evidence_url": url}


def people_from_lines(html: str, domain: str = "", site_name: str = "", url: str = "") -> list[dict]:
    """A name on one line and a job title on the next (or "Name, Title" on
    one line). Titles that are really bios, or that belong to another
    company (a quoted customer), are rejected."""
    lines = page_lines(drop_quoted(html))
    found, seen = [], set()
    for i, line in enumerate(lines):
        candidates = []
        prev = lines[i - 1] if i else ""
        if _QUOTE_LINE.search(prev) or re.match(r"^[—–-]\s*\S", line):
            continue        # a customer quote and its attribution
        name = split_person_name(line)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if name and _reads_like_title(nxt) and not nxt.rstrip().endswith(("!", "?")):
            candidates.append((name, nxt, line))
        m = _SEPARATED.match(line)
        if m and not candidates:
            name2 = split_person_name(m.group("name"))
            title = m.group("title").strip()
            if name2 and _reads_like_title(title) and _STRONG_ROLE.search(title) \
                    and not title.endswith(".") and len(title.split()) <= 7:
                candidates.append((name2, title, m.group("name").strip()))
        for (first, last), title, raw in candidates:
            key = (first.lower(), last.lower())
            if key in seen:
                continue
            if title_names_other_org(title, domain, site_name):
                continue
            seen.add(key)
            found.append(_person(first, last, title, source="card", full_name=raw,
                                 evidence=f"{raw} — {title}", url=url))
    return found


# ── people in schema.org ─────────────────────────────────────────────────

_ORG_KEYS = (("founder", "Founder"), ("founders", "Founder"), ("employee", ""),
             ("employees", ""), ("member", ""), ("members", ""), ("owner", "Owner"))


def _json_blocks(html: str) -> list:
    out = []
    for raw in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or ""):
        try:
            out.append(json.loads(raw.strip()))
        except (ValueError, TypeError):
            continue
    return out


def _nodes(data):
    if isinstance(data, list):
        for item in data:
            yield from _nodes(item)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity", "about", "publisher", "provider", "brand", "parentOrganization"):
            if key in data:
                yield from _nodes(data[key])


def _types(node) -> list[str]:
    t = node.get("@type", "")
    return [x.lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def people_from_schema(html: str, url: str = "") -> list[dict]:
    """founder / employee / member of the business's own Organization (or
    LocalBusiness, Dentist, LegalService...) node. A Person who is only an
    article's author is a blogger, not staff, and is ignored."""
    found, seen = [], set()
    for block in _json_blocks(html):
        nodes = list(_nodes(block))
        by_id = {n.get("@id"): n for n in nodes if isinstance(n.get("@id"), str)}
        for node in nodes:
            types = _types(node)
            if not types or any(t in ("person", "article", "blogposting", "newsarticle", "webpage",
                                      "website", "imageobject", "breadcrumblist", "review")
                                for t in types):
                continue
            for key, default_title in _ORG_KEYS:
                values = node.get(key)
                if values is None:
                    continue
                for v in values if isinstance(values, list) else [values]:
                    if isinstance(v, dict) and set(v) <= {"@id"} and v.get("@id") in by_id:
                        v = by_id[v["@id"]]
                    name, title = "", default_title
                    if isinstance(v, str):
                        name = v
                    elif isinstance(v, dict):
                        if v.get("@type") and "person" not in _types(v):
                            continue
                        name = v.get("name") or " ".join(
                            x for x in (v.get("givenName"), v.get("familyName")) if isinstance(x, str))
                        jt = v.get("jobTitle")
                        if isinstance(jt, str) and jt.strip():
                            title = jt.strip()
                    split = split_person_name(name if isinstance(name, str) else "")
                    if not split:
                        continue
                    key_ = (split[0].lower(), split[1].lower())
                    if key_ in seen:
                        continue
                    seen.add(key_)
                    owner = default_title in ("Founder", "Owner") or is_owner_title(title)
                    found.append(_person(*split, title or "Team member", source="schema", full_name=name,
                                         evidence=f"schema.org {key}: {name}", url=url, owner=owner))
    found.sort(key=lambda p: -owner_rank(p["title"], p["owner"]))
    return found


# ── owners named in prose ────────────────────────────────────────────────

_CAP = r"[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)*"
_NAME_RE = (rf"(?:Dr\.?\s+)?(?P<name>{_CAP}(?:\s+[A-Z]\.)?(?:\s+(?:{'|'.join(sorted(_PARTICLES))}))*"
            rf"\s+{_CAP}(?:\s+{_CAP})?)")
_OWNER_WORD = (r"(?P<title>(?i:co-?owner|owner|co-?founder|founder|proprietor|president|ceo|"
               r"principal|managing partner|founding partner|founding attorney|partners?))")
_ADJ = r"(?:(?:our|the|its|current|proud|longtime|second[- ]generation|third[- ]generation|" \
       r"original|today'?s?|new)\s+)*"
_PROSE = (
    # "founded by (attorney) Colin Garrity", "started by Dr. Ann Lee"
    (re.compile(rf"(?i:\b(?:founded|started|established|launched|opened|created)\s+(?:in\s+\d{{4}}\s+)?by)\s+"
                rf"(?:(?i:attorney|dentist|our\s+founder|owner|president)\s+)?{_NAME_RE}"), "Founder"),
    # "owned and operated by Gary Pruitt"
    (re.compile(rf"(?i:\b(?:owned\s+and\s+operated|owned|operated)\s+(?:today\s+)?by)\s+"
                rf"(?!(?i:president|owner|ceo|founder)\b){_NAME_RE}"), "Owner"),
    # "with second-generation owner Dale Whitlock", "Led today by President
    # Ilan Brooks". The title must follow words that make it this business's
    # ("our", "by", "with"...): "KTR President Vic Hale", "Acme CEO Sarah
    # Connor" and "President Alma Reyes visited" are someone else's. No comma:
    # "Founder, Birchwood Realty" under a testimonial names a company.
    (re.compile(rf"(?i:\b(?:by|with|as|our|its|current|proud|longtime|(?:second|third|fourth)[- ]generation|"
                rf"today'?s)\s+{_ADJ}){_OWNER_WORD}\s+{_NAME_RE}"), None),
    # "our founder, Owen Whitlock"
    (re.compile(rf"(?i:\b(?:our|the\s+company'?s|its)\s+){_OWNER_WORD},\s+{_NAME_RE}"), None),
    # "Dale Whitlock, owner", "Hal Brandt , founder of Tallyway"
    (re.compile(rf"{_NAME_RE}\s*,\s*(?i:{_ADJ}){_OWNER_WORD}\b(?P<tail>.{{0,40}})"), None),
    # "Ines Varga started Countwise", "Abel Harrow founded Harrow Construction"
    (re.compile(rf"{_NAME_RE}\s+(?i:co-?founded|founded|started|launched|established|opened)\s+"
                rf"(?P<org>(?i:the\s+(?:company|business|firm|practice|agency))|[A-Z][\w&'’.-]*(?:\s+(?:[A-Z][\w&'’.-]*|&))*)"),
     "Founder"),
    # "the office of Dr. Maren Hollis Pryor", "the practice of Dr. ..."
    (re.compile(rf"(?i:\b(?:the\s+)?(?:office|practice|clinic)\s+of)\s+Dr\.?\s+{_NAME_RE}"), "Owner"),
)
_FIRST_PERSON = re.compile(r"(?i)\bI\s+(?:founded|started|opened|own|established|launched)\b")
# "Renata founded Ashby & Kerr", "Joelle started the Penrose & Vance Law Firm":
# a first name only, resolved against the full names the site lists.
_FIRST_NAME_FOUNDER = re.compile(
    rf"\b(?P<first>{_CAP})\s+(?i:co-?founded|founded|started|launched|established|opened)\s+"
    rf"(?P<org>(?:the\s+)?[A-Z][\w&'’.-]*(?:\s+(?:[A-Z][\w&'’.-]*|&))*)")


_ROLE_ONLY = re.compile(r"(?i)(?:and\s+)?(?:ceo|cfo|coo|cto|cmo|president|owner|founder|co-?founder|"
                       r"principal|partner|chief\s+\w+(?:\s+officer)?|general manager)")
# A person the site remembers rather than employs.
_DEPARTED = r"(?:passed away|died|retired|was laid to rest|is missed|rest in peace)"


def _departed(person: dict, text: str) -> bool:
    first, last = re.escape(person["first_name"]), re.escape(person["last_name"].split()[-1])
    return bool(re.search(
        rf"(?i)(?:\bthe late|in (?:loving )?memory of|remembering)\s+(?:dr\.?\s+)?{first}\b"
        rf"|\b{first}\b(?:\s+\w+\.?){{0,3}}?(?:\s+{last})?\s*,?[^.]{{0,40}}?\b{_DEPARTED}", text))


def _title_word(word: str) -> str:
    low = word.lower()
    if low in ("ceo",):
        return "CEO"
    if low.startswith("partner"):
        return "Partner"
    return "-".join(p.capitalize() for p in low.split("-")).replace("Co-Founder", "Co-founder")


def people_from_prose(html: str, domain: str, site_name: str = "", url: str = "") -> list[dict]:
    """Owners and founders named in a sentence on the business's own site."""
    html = drop_quoted(html)
    found, seen = [], set()

    def add(raw_name, title, sentence):
        split = split_person_name(raw_name)
        if not split:
            # A third capitalised word may be the start of the next phrase
            # ("Roy Danner President"): retry on the first two words.
            words = raw_name.split()
            split = split_person_name(" ".join(words[:2])) if len(words) > 2 else None
        if not split:
            return
        key = (split[0].lower(), split[1].lower())
        if key in seen:
            return
        seen.add(key)
        found.append(_person(*split, title, source="prose", evidence=sentence, url=url, full_name=raw_name,
                             owner=is_owner_title(title)))

    for sentence in _sentences(html):
        for regex, fixed in _PROSE:
            for m in regex.finditer(sentence):
                groups = m.groupdict()
                title = fixed or _title_word(groups.get("title") or "")
                if "org" in groups and groups["org"] and not groups["org"].lower().startswith("the "):
                    if not names_business(groups["org"], domain, site_name):
                        continue
                tail = groups.get("tail")
                if tail is None:
                    # "Principal Mary Alcott of Maplewood Elementary"
                    tail = sentence[m.end():m.end() + 60]
                    tail = tail if re.match(r"^\s+(?:of|at)\s", tail) else ""
                # "founder of Tallyway" is theirs; "founder Signary" and
                # "Owner, Davis Bakery" (a customer's own business) are not.
                t = tail.strip().lstrip(",;:-–—").strip()
                m_org = re.match(r"^(?:(?:of|at)\s+|@\s*)?((?:the\s+)?[A-Z][\w&'’.-]*(?:\s+[A-Z][\w&'’.-]*)*)", t)
                if m_org and not _ROLE_ONLY.fullmatch(m_org.group(1)) \
                        and not names_business(m_org.group(1), domain, site_name):
                    continue
                add(groups["name"], title, sentence)

    # "I founded the Fairbanks Law Firm" on a bio page: the person is the one
    # the page is about, named in its <title> or first heading.
    sentences = _sentences(html)
    listed = {}
    for line in page_lines(html):
        split = split_person_name(line)
        if split:
            listed.setdefault(split[0].lower(), set()).add((split, line))
    for sentence in sentences:
        for m in _FIRST_NAME_FOUNDER.finditer(sentence):
            matches = listed.get(m.group("first").lower(), set())
            if len({s for s, _ in matches}) == 1 and names_business(m.group("org"), domain, site_name):
                (split, line), = list(matches)[:1]
                add(line, "Founder", sentence)

    if _FIRST_PERSON.search(" ".join(sentences)):
        subject = ""
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
        h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html or "")
        for raw in ([m.group(1)] if m else []) + ([h1.group(1)] if h1 else []):
            text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
            head = re.split(r"\s[|–—-]\s|\|", text)[0]
            head = re.sub(r"(?i)^\s*(?:meet\s+)?(?:attorney|dr\.?|doctor|lawyer|founder|owner)\s+", "", head)
            if split_person_name(head):
                subject = head
                break
        if subject:
            add(subject, "Founder", "first-person founder on a bio page")

    found.sort(key=lambda p: -owner_rank(p["title"], p["owner"]))
    return found


# ── everything on a site ─────────────────────────────────────────────────

def find_people(pages: list[tuple[str, str]], domain: str, site_name: str = "") -> list[dict]:
    """Every person the site names, merged across pages and ranked so the
    likeliest decision maker comes first.

    Sources: cards/lists (profile.detect_people), schema.org, prose. People
    the site remembers ("the late Harold Smith", "retired in 2010") are
    dropped, and so is a founder known only from a history sentence
    ("Founded by Owen Whitlock in the 1970s") when the site also lists a
    current owner.
    """
    from harvey.collectors.profile import ProfileCollector

    detect = ProfileCollector(state=None).detect_people
    merged: dict[tuple[str, str], dict] = {}
    order = []
    for url, html in pages:
        batches = [
            [dict(p, source="card", owner=False, evidence=f"{p['full_name']} — {p['title']}",
                  evidence_url=url) for p in detect(html, domain=domain, site_name=site_name)],
            people_from_schema(html, url=url),
            people_from_prose(html, domain, site_name, url=url),
        ]
        for batch in batches:
            for p in batch:
                key = (p["first_name"].lower(), p["last_name"].split()[-1].lower())
                p = {k: v for k, v in p.items() if k != "is_decision_maker"}
                p["owner"] = bool(p.get("owner")) or is_owner_title(p["title"])
                if key not in merged:
                    merged[key] = p
                    order.append(key)
                    continue
                cur = merged[key]
                # A card's title is the business's own label; prose and
                # schema only fill in what the card lacks.
                better = owner_rank(p["title"], p["owner"]) > owner_rank(cur["title"], cur["owner"])
                weak = owner_rank(cur["title"]) < 40     # no recognisable role in the title
                if (cur["source"] != "card" and p["source"] == "card") \
                        or (better and (cur["source"] != "card" or p["source"] == "card" or weak)):
                    for k in ("title", "evidence", "evidence_url", "source"):
                        cur[k] = p[k]
                cur["owner"] = cur["owner"] or p["owner"]
    people = [merged[k] for k in order]
    text = " ".join(" ".join(_sentences(html))[:200_000] for _, html in pages)
    people = [p for p in people if not _departed(p, text)]
    current_owner = any(p["source"] != "prose" and p["owner"] for p in people)
    if current_owner:
        people = [p for p in people
                  if not (p["source"] == "prose" and p["title"] == "Founder"
                          and re.search(r"(?i)\bfounded\b[^.]*\bby\b|\bour founder\b", p["evidence"]))]
    for p in people:
        # Explicit current owner titles on the business's own cards first;
        # a surname in the business name only breaks a tie between owners,
        # it never makes anyone an owner (Linda Smith, Office Manager, at
        # Smith & Sons is not).
        surnames = " ".join(p.get("full_name", p["last_name"]).split()[1:]) or p["last_name"]
        p["rank"] = (owner_rank(p["title"], p["owner"])
                     + (3 if p["source"] == "card" and is_owner_title(p["title"]) else 0)
                     + (2 if p["owner"] and surname_on_the_door(surnames, domain, site_name) else 0)
                     - (5 if p["source"] == "prose" else 0))
    people.sort(key=lambda p: -p["rank"])
    return people


# ── which pages to read ──────────────────────────────────────────────────

_LINK_WEIGHTS = (
    (3, r"team|meet|leadership|staff|people|doctors?|dentists?|attorneys?|"
        r"lawyers?|physicians?|providers?|owners?|founders?|principals?|partners|who-?we-?are|bios?"),
    (2, r"about|story|history|company|firm|practice|family"),
    (1, r"contact"),
)
# A keyword counts in a whole path segment ("/our-team/", "/attorneys/",
# "/meet-the-dentists.html") or a short menu label ("Meet the Team") — not
# inside a practice-area slug ("/child-custody-lawyer-riverton/").
_SKIP_LINK = re.compile(
    r"(?i)\.(?:pdf|jpe?g|png|gif|webp|svg|zip|docx?|mp4|mp3)(?:$|\?)|/(?:blog|news|posts?|articles?|"
    r"category|tag|author|feed|wp-json|cart|checkout|login|account|careers?|jobs?)/|/\d{4}/\d{2}/|"
    r"[?&](?:replytocom|share)=|/:param")


def _same_site(a: str, b: str) -> bool:
    return a.lower().removeprefix("www.") == b.lower().removeprefix("www.")


def _norm_url(url: str) -> str:
    p = urlparse(url)
    path = p.path.rstrip("/") or "/"
    return f"{p.netloc.lower().removeprefix('www.')}{path}{('?' + p.query) if p.query else ''}"


def menu_links(html: str, page_url: str, limit: int = 5) -> list[str]:
    """The site's own links to the pages where its people are named: team,
    meet-the-doctors, about, our-story, attorneys, contact — scored by
    keywords in the link's path and text. Same site only, never the
    homepage itself, files, blog posts or careers pages."""
    base = urlparse(page_url)
    home = _norm_url(page_url)
    scored, seen = [], {home}
    for i, m in enumerate(re.finditer(r'(?is)<a\b[^>]*?href\s*=\s*["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>',
                                      _strip_code(html or ""))):
        href = html_lib.unescape(m.group(1)).strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "sms:")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not _same_site(p.netloc, base.netloc):
            continue
        if _SKIP_LINK.search(url):
            continue
        key = _norm_url(url)
        if key in seen or p.path.rstrip("/") in ("", "/index.html", "/index.php", "/home"):
            continue
        text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(2)))).strip().lower()
        segments = [re.sub(r"\.(?:html?|php|aspx?)$", "", seg) for seg in unquote(p.path).lower().split("/") if seg]
        label = text if len(text.split()) <= 4 else ""
        score = 0
        for weight, pattern in _LINK_WEIGHTS:
            whole = rf"^(?:our-|the-|meet-(?:the-|our-)?|about-)?(?:{pattern})(?:-(?:us|team|page|bios?|staff))?$"
            if any(re.search(whole, seg) for seg in segments) \
                    or (segments and re.match(r"^meet-", segments[-1]) and weight == 3) \
                    or re.search(rf"(?:^|[^a-z])(?:{pattern})(?:$|[^a-z])", label):
                score = max(score, weight)
        if not score:
            continue
        seen.add(key)
        scored.append((-score, len(p.path), i, url))
    scored.sort()
    return [url for *_, url in scored[:limit]]


# ── addresses on a page ──────────────────────────────────────────────────

# Bounded on both sides and anchored at the start of a run, so a page of
# 1.5MB of hex or dotted text is scanned in linear time.
EMAIL_RE = re.compile(
    r"(?<![a-z0-9._%+\-])[a-z0-9][a-z0-9._%+\-]{0,63}@"
    r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?){0,6}\.[a-z]{2,24}"
    r"(?![a-z0-9\-])", re.I)
_JUNK_DOMAINS = ("example.com", "example.org", "domain.com", "email.com", "yourdomain.com",
                 "mail.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
                 "yoursite.com", "website.com", "company.com", "test.com")
_JUNK_TLDS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".js", ".css")
# Obfuscations people use to hide an address from scrapers:
#   name [at] domain [dot] com, name(at)domain.com, name AT domain DOT com
# "name [at] domain [dot] com", "lou (at) acme (dot) com": only a bracketed
# "at", never the word in prose ("our office at Main dot Street"), and only
# a real top-level domain.
_AT_DOT = re.compile(
    r"(?<![a-z0-9._%+\-])([a-z0-9][a-z0-9._%+\-]{0,63})\s{0,3}(?:\[\s{0,3}at\s{0,3}\]|\(\s{0,3}at\s{0,3}\)|\{\s{0,3}at\s{0,3}\})\s{0,3}"
    r"([a-z0-9\-]{1,63}(?:\s{0,3}(?:\[\s{0,3}dot\s{0,3}\]|\(\s{0,3}dot\s{0,3}\)|\{\s{0,3}dot\s{0,3}\}|\.)\s{0,3}[a-z0-9\-]{1,63}){1,4})", re.I)
_KNOWN_TLDS = frozenset("""
com net org edu gov io co us biz info me ca uk au nz ie de fr es it nl be ch at se no dk fi
pl pt mx br in ai app dev law legal dental agency marketing design studio tech solutions
services company group pro health care clinic roofing law
""".split())
# Recognised obfuscation schemes only: a mailto-ish element carrying a local
# part and a domain (data-user/data-domain; data-front/data-back, as WordPress
# team plugins emit). Generic pairs (data-name + data-website) are not emails.
_DATA_PAIR_LOCAL = ("data-user", "data-front", "data-mailbox")
_DATA_PAIR_DOMAIN = ("data-domain", "data-back")


def _cf_decode(hexstr: str) -> str:
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr) - 1, 2))
    except ValueError:
        return ""


def _clean_address(addr: str) -> str:
    addr = unquote(html_lib.unescape(addr or "")).strip().strip(".,;:<>()[]'\"").lower()
    addr = addr.removeprefix("mailto:").split("?")[0].strip()
    if not EMAIL_RE.fullmatch(addr):
        return ""
    domain = addr.rpartition("@")[2]
    if domain in _JUNK_DOMAINS or domain.endswith(_JUNK_TLDS) or re.search(r"@\d+x\.", addr):
        return ""
    return addr


MAX_PAGE_CHARS = 1_500_000   # a sane cap per page; real pages are far smaller


def emails_on_page(html: str) -> list[str]:
    """Every address a page publishes, including the common obfuscations:
    Cloudflare email protection, data-attribute pairs (data-front="rashby"
    data-back="ashbykerr.test"), "name [at] domain [dot] com", entities."""
    html = (html or "")[:MAX_PAGE_CHARS]
    found: list[str] = []

    def add(addr):
        addr = _clean_address(addr)
        if addr and addr not in found:
            found.append(addr)

    for m in re.finditer(r'(?i)href\s*=\s*["\']\s*mailto:([^"\'>]+)["\']', html):
        add(m.group(1))
    # schema.org "email" (in a script, so the text scan below never sees it)
    for raw in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        for m in re.finditer(r'"email"\s*:\s*"([^"]+)"', raw):
            add(m.group(1))
    for hexstr in re.findall(r'(?i)data-cfemail\s*=\s*["\']([0-9a-f]{4,})["\']', html):
        add(_cf_decode(hexstr))
    for hexstr in re.findall(r"(?i)/cdn-cgi/l/email-protection#([0-9a-f]{4,})", html):
        add(_cf_decode(hexstr))
    for tag in re.finditer(r"(?is)<[a-z][^>]*\bdata-[a-z\-]+\s*=[^>]*>", html):
        raw_tag = tag.group(0)
        mailish = re.match(r"(?i)<a\b", raw_tag) or re.search(
            r"""(?i)\b(?:class|id|title|aria-label)\s*=\s*["'][^"']*mail""", raw_tag)
        if not mailish:
            continue
        attrs = dict((k.lower(), v) for k, v in re.findall(r'(?i)\b(data-[a-z\-]+)\s*=\s*["\']([^"\']*)["\']', raw_tag))
        local = next((attrs[k] for k in _DATA_PAIR_LOCAL if k in attrs), "")
        domain = next((attrs[k] for k in _DATA_PAIR_DOMAIN if k in attrs), "")
        if local and domain and re.fullmatch(r"[a-z0-9._%+\-]+", local, re.I) \
                and re.fullmatch(r"[a-z0-9\-]+(?:\.[a-z0-9\-]+)+", domain, re.I):
            add(f"{local}@{domain}")
    text = unquote(html_lib.unescape(_strip_code(html)))
    for m in EMAIL_RE.finditer(text):
        add(m.group(0))
    plain = re.sub(r"(?s)<[^>]+>", " ", text)
    for m in _AT_DOT.finditer(plain):
        domain = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\})\s*", ".", m.group(2), flags=re.I)
        domain = domain.replace(" ", "").lower()
        if domain.rpartition(".")[2] in _KNOWN_TLDS:
            add(f"{m.group(1)}@{domain}")
    return found
