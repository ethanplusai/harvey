"""Selling services: the general playbook, and an angle per industry.

A firm that sells services (software and AI development, consulting,
accounting, design, legal) offers each industry something different: claims
automation to an insurer, middleware across a CPA firm's tools, product design
or extra engineers to a SaaS company. ``product.offering: services`` says the
operator is such a firm. Then the writer and the reply handler use
``skills/selling_services.md``, and, when the prospect's industry matches one,
that industry's section of the operator's plays file (``writer.industry_plays``).

The plays file is the operator's own markdown, one ``## Industry`` section per
industry. ``harvey train`` drafts it from the operator's website; the operator
edits it. Two optional lines in a section are read by Harvey, and the rest of
the section goes into the prompt as written:

    ## Insurance
    match: insurance, insurer, carrier, underwriting
    roles: claims processor, claims adjuster

``match:`` words find the section for a prospect (whole words, any case; the
section's title counts too). ``roles:`` are job titles whose openings mean that
industry may need the operator's help; they join ``icp.hiring_signals`` when
profiling reads a company's job board.

Campaigns are already grouped by industry, so each industry's campaign gets
its own angle. A missing or unreadable plays file is reported and skipped:
plays add an angle, they never replace the writing rules.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from harvey.paths import PROJECT_ROOT
from harvey.textio import read_user_text

logger = logging.getLogger("harvey.industry_plays")

OFFERINGS = ("product", "services")
SERVICES_SKILL = "selling_services"
DEFAULT_PLAYS_PATH = "skills/industry_plays.md"
SUGGESTED_PLAYS_PATH = "skills/industry_plays.suggested.md"

# A whole plays file, and one play in a prompt. Bigger than this is probably
# the wrong file, and would crowd out the facts about the prospect.
MAX_PLAYS_CHARS = 30_000
MAX_PLAY_CHARS = 3_000
MAX_ROLES_PER_PLAY = 8

_HEADER_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$", re.M)
_META_RE = re.compile(r"^\s*(match|roles)\s*:\s*(.*)$", re.I)


class IndustryPlaysError(Exception):
    """writer.industry_plays names a file Harvey can't use."""


@dataclass
class Play:
    """One industry's section of the plays file."""

    name: str
    match: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    body: str = ""

    def terms(self) -> list[str]:
        """What identifies this industry: its match words and its title."""
        out = [t for t in self.match]
        title = self.name.strip().lower()
        if title and title not in out:
            out.append(title)
        return out


def _split_list(value: str) -> list[str]:
    seen: list[str] = []
    for item in value.split(","):
        item = " ".join(item.split()).strip().lower()
        if item and item not in seen:
            seen.append(item)
    return seen


def parse_plays(text: str) -> list[Play]:
    """The sections of a plays file, in file order. Text before the first
    ``## `` heading (an introduction) is ignored, and a section with no
    body and no match words is dropped."""
    text = text or ""
    headers = list(_HEADER_RE.finditer(text))
    plays: list[Play] = []
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        name = h.group(1).strip()
        match: list[str] = []
        roles: list[str] = []
        body_lines: list[str] = []
        for line in text[h.end():end].splitlines():
            meta = _META_RE.match(line)
            if meta:
                (match if meta.group(1).lower() == "match" else roles).extend(
                    _split_list(meta.group(2)))
            else:
                body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if not name or not (body or match):
            continue
        plays.append(Play(name=name, match=list(dict.fromkeys(match)),
                          roles=list(dict.fromkeys(roles))[:MAX_ROLES_PER_PLAY], body=body))
    return plays


def _term_re(term: str) -> re.Pattern:
    """Whole words, any case, with a plural ending allowed ("insurer"
    matches "insurers", "cpa" matches "CPAs", "accounting firm" matches
    "accounting firms"); "insurer" still doesn't match "reinsurers"."""
    words = [re.escape(w) for w in term.split()]
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-/&]+".join(words)
                      + r"(?:e?s)?(?![a-z0-9])", re.I)


def _mentions(text: str, term: str) -> bool:
    return bool(text and term and _term_re(term).search(text))


def match_play(plays: list[Play], industry: str = "", context: str = "") -> Play | None:
    """The play for a prospect, or None.

    ``industry`` (the prospect's or company's industry) is tried first, across
    every play, so a clear industry label always beats a passing word in a
    description. Only when no play matches it is ``context`` tried (the
    company's description and the tools on its site). File order breaks ties.
    """
    for text in (industry, context):
        if not text:
            continue
        for play in plays:
            if any(_mentions(text, term) for term in play.terms()):
                return play
    return None


def play_for_prospect(plays: list[Play], prospect, company=None) -> Play | None:
    """match_play with a prospect and its company: their industry fields
    first, then the company's description and the tools on its site."""
    if not plays:
        return None
    industry = " ".join(x for x in (getattr(company, "industry", "") or "",
                                    getattr(prospect, "industry", "") or "") if x)
    context = " ".join([getattr(company, "description", "") or ""]
                       + [str(t) for t in (getattr(company, "tech_stack", None) or [])])
    return match_play(plays, industry, context)


def offering_of(config) -> str:
    """``product.offering``, "product" when absent or unknown."""
    value = getattr(getattr(config, "product", None), "offering", "") or "product"
    return value if value in OFFERINGS else "product"


def sells_services(config) -> bool:
    return offering_of(config) == "services"


def plays_path(config) -> str:
    """writer.industry_plays, or skills/industry_plays.md when that is unset
    and the file exists (so copying the example into place is enough)."""
    raw = str(getattr(getattr(config, "writer", None), "industry_plays", "") or "").strip()
    if not raw and (PROJECT_ROOT / DEFAULT_PLAYS_PATH).is_file():
        return DEFAULT_PLAYS_PATH
    return raw


def _resolve(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_plays(config) -> list[Play]:
    """The operator's plays, or [] when none are set.

    Raises IndustryPlaysError when a file is set but missing, unreadable,
    empty, too long, or has no ``## `` sections.
    """
    raw = plays_path(config)
    if not raw:
        return []
    try:
        text = read_user_text(_resolve(raw)).strip()
    except FileNotFoundError:
        raise IndustryPlaysError(f"{raw} doesn't exist") from None
    except OSError as e:
        raise IndustryPlaysError(f"{raw} can't be read ({e})") from None
    if not text:
        raise IndustryPlaysError(f"{raw} is empty")
    if len(text) > MAX_PLAYS_CHARS:
        raise IndustryPlaysError(
            f"{raw} is {len(text):,} characters; the limit is {MAX_PLAYS_CHARS:,}")
    plays = parse_plays(text)
    if not plays:
        raise IndustryPlaysError(f"{raw} has no '## Industry' sections")
    return plays


def load_plays_quietly(config) -> list[Play]:
    """load_plays, logging a problem instead of raising: a broken plays file
    costs the angle, never the email."""
    try:
        return load_plays(config)
    except IndustryPlaysError as e:
        logger.warning(f"Industry plays (writer.industry_plays) skipped: {e}")
        return []


def hiring_keywords(config) -> list[str]:
    """icp.hiring_signals (or icp.titles), plus every play's ``roles:``.
    Used where profiling reads job boards; never raises."""
    from harvey.collectors.ats import role_keywords

    keywords = list(role_keywords(getattr(config, "icp", None)))
    if config is None or not sells_services(config):
        return keywords
    for play in load_plays_quietly(config):
        for role in play.roles:
            if role not in keywords:
                keywords.append(role)
    return keywords


def play_block(play: Play | None) -> str:
    """One play as a prompt section ("" for None)."""
    if play is None:
        return ""
    body = play.body[:MAX_PLAY_CHARS]
    return (
        f"\n\nINDUSTRY PLAY — {play.name}\n"
        "The operator's own notes on what they offer this industry. Use them to pick\n"
        "the angle. Claim only the proof written here, and only facts about the\n"
        "prospect that the FACTS list. Never state the industry's problems as this\n"
        "prospect's own unless the facts show it:\n"
        + body
    )


def services_block(brain) -> str:
    """skills/selling_services.md as a prompt section ("" if absent)."""
    load = getattr(brain, "load_skill", None)
    text = load(SERVICES_SKILL) if load is not None else ""
    return "\n\nSELLING SERVICES\n" + text if text else ""


# ── what training drafts ──

_MAX_FIELD = 240
_MAX_ITEMS = 5
MAX_TRAINED_PLAYS = 6


def _clean(value, limit: int = _MAX_FIELD) -> str:
    text = " ".join(str(value or "").split())
    # A heading or a meta line inside a value would split or rewrite a section.
    text = re.sub(r"^#+\s*", "", text)
    return text[:limit].rstrip()


def _items(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        c = _clean(v)
        if c and c not in out:
            out.append(c)
        if len(out) >= _MAX_ITEMS:
            break
    return out


def render_trained_plays(plays, company: str = "") -> str:
    """Markdown for skills/industry_plays.md from what training drafted
    (a list of dicts), or "" when nothing usable came back."""
    if not isinstance(plays, list):
        return ""
    sections: list[str] = []
    seen: set[str] = set()
    for p in plays:
        if not isinstance(p, dict):
            continue
        name = _clean(p.get("industry"), 60)
        if not name or name.lower() in seen:
            continue
        services = _items(p.get("services"))
        if not services:
            continue
        seen.add(name.lower())
        match = [m.lower() for m in _items(p.get("match")) if "," not in m]
        roles = [r.lower() for r in _items(p.get("roles")) if "," not in r]
        lines = [f"## {name}"]
        if match:
            lines.append("match: " + ", ".join(match))
        if roles:
            lines.append("roles: " + ", ".join(roles))
        lines.append("")
        lines.append("**What we offer them:**")
        lines += [f"- {s}" for s in services]
        pains = _items(p.get("pains"))
        if pains:
            lines += ["", "**Problems this industry often has (not proof about any one company):**"]
            lines += [f"- {x}" for x in pains]
        proof = _items(p.get("proof"))
        lines += ["", "**Proof (only as our website states it):**"]
        lines += [f"- {x}" for x in proof] if proof else ["- None listed. Don't claim any."]
        avoid = _items(p.get("avoid"))
        if avoid:
            lines += ["", "**Don't say:**"]
            lines += [f"- {x}" for x in avoid]
        sections.append("\n".join(lines))
        if len(sections) >= MAX_TRAINED_PLAYS:
            break
    if not sections:
        return ""
    who = f" for {company}" if company else ""
    intro = (
        f"# Industry plays{who}\n\n"
        "Drafted by `harvey train` from your website. Review every line before\n"
        "Harvey writes with it: the writer and reply handler use the section that\n"
        "matches each prospect's industry. `match:` words find the section, and\n"
        "`roles:` are job openings that suggest a company needs you. Edit freely;\n"
        "retraining never overwrites this file.\n"
    )
    return intro + "\n\n" + "\n\n".join(sections) + "\n"


def save_trained_plays(text: str, root: Path | None = None) -> Path:
    """Write drafted plays without touching the operator's own file: to
    skills/industry_plays.md when it doesn't exist, else to
    skills/industry_plays.suggested.md. Returns the path written."""
    root = Path(root) if root is not None else PROJECT_ROOT
    main = root / DEFAULT_PLAYS_PATH
    target = main if not main.exists() else root / SUGGESTED_PLAYS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
