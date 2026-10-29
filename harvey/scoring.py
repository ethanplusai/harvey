"""Deterministic, explainable lead scoring.

The same inputs always give the same score, and every point is attributed to
a named part with a reason, so a score can be explained and argued with. The
weights come from ``scoring.weights`` in harvey.yaml; the defaults reproduce
the Scout's original ICP-fit heuristic exactly, and ``geography`` and
``tech_fit`` are off (0) until the user gives them weight.

The Scout's Claude pass may still adjust the stored score; this is the
baseline it starts from and the fallback when Claude is unavailable.
"""

import re
from dataclasses import dataclass, field

from harvey.collectors import people as people_mod

DEFAULT_WEIGHTS: dict[str, float] = {
    "base": 40,        # every found contact starts here
    "title": 25,       # title matches icp.titles (or any senior title)
    "seniority": 20,   # c-suite gets all of it, then VP, director, manager
    "industry": 10,    # industry matches icp.industries
    "email": 8,        # verified address; a catch-all or guess earns less
    "hiring": 10,      # a timely hiring signal
    "geography": 0,    # company location matches icp.geography
    "tech_fit": 0,     # tools detected on their site
}
SENIORITY_SHARE = {"c_suite": 1.0, "vp": 0.75, "director": 0.5, "manager": 0.25}
EMAIL_SHARE = {"verified": 1.0, "risky": 3 / 8}
# Any other address on file earns this share of the email weight.
OTHER_EMAIL_SHARE = 1 / 8
# Tools that earn the full tech_fit weight.
TECH_FIT_FULL_AT = 5
MIN_SCORE, MAX_SCORE = 1, 100

# With no icp.titles, any of these counts as a title match.
SENIOR_KEYWORDS = (
    "ceo", "cto", "cfo", "cmo", "coo", "cro", "chief", "founder",
    "co-founder", "president", "owner", "partner", "vp", "vice president",
    "svp", "evp", "head of", "director",
)
HIRING_NOTE = "Signal: hiring"


@dataclass(frozen=True)
class ScorePart:
    name: str
    points: float
    weight: float
    reason: str


@dataclass(frozen=True)
class ScoreResult:
    total: int
    parts: list[ScorePart] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "parts": [
                {"name": p.name, "points": round(p.points, 1), "weight": p.weight,
                 "reason": p.reason}
                for p in self.parts
            ],
        }


def merged_weights(overrides: dict[str, float] | None) -> dict[str, float]:
    return {**DEFAULT_WEIGHTS, **(overrides or {})}


def title_fits_icp(title: str, icp_titles: list[str]) -> bool:
    """Word-based title match (see people.title_matches); any senior title
    counts when the ICP names none."""
    if not title:
        return False
    if icp_titles:
        return people_mod.title_matches(title, icp_titles)
    lowered = title.lower()
    return any(re.search(rf"(?<![\w-]){re.escape(kw)}\b", lowered) for kw in SENIOR_KEYWORDS)


def _industry_fits(industry: str, targets: list[str]) -> bool:
    ind = (industry or "").lower()
    return bool(ind) and any(t.lower() in ind or ind in t.lower() for t in targets if t)


def _geography_fits(location: str, targets: list[str]) -> bool:
    """A target's place name ("Denver" of "Denver, CO") appears in the location."""
    loc = (location or "").lower()
    if not loc:
        return False
    for target in targets:
        place = (target or "").split(",")[0].strip().lower()
        if place and place in loc:
            return True
    return False


def _has_hiring_signal(notes: str, company_signals: list[dict]) -> bool:
    if HIRING_NOTE in (notes or ""):
        return True
    return any(str(s.get("type", "")).lower() == "hiring" for s in company_signals or [])


def score_prospect(prospect, icp, weights: dict[str, float] | None = None,
                   company=None, title_match: bool | None = None) -> ScoreResult:
    """Score one contact against the ICP. ``title_match`` lets a caller with
    its own matcher (the Scout) pass the answer in; ``company`` adds
    geography and tech fit when it's known."""
    w = merged_weights(weights)
    titles = getattr(icp, "titles", None) or []
    industries = getattr(icp, "industries", None) or []
    geography = getattr(icp, "geography", None) or []
    parts: list[ScorePart] = [ScorePart("base", w["base"], w["base"], "starting point")]

    fits = title_fits_icp(prospect.title, titles) if title_match is None else title_match
    parts.append(ScorePart(
        "title", w["title"] if fits else 0, w["title"],
        f"'{prospect.title}' matches the target titles" if fits
        else f"'{prospect.title or 'no title'}' isn't a target title"))

    share = SENIORITY_SHARE.get(prospect.seniority, 0.0)
    parts.append(ScorePart(
        "seniority", w["seniority"] * share, w["seniority"],
        f"seniority: {prospect.seniority or 'unknown'}"))

    industry = prospect.industry or (company.industry if company else "")
    fits = bool(industries) and _industry_fits(industry, industries)
    parts.append(ScorePart(
        "industry", w["industry"] if fits else 0, w["industry"],
        f"industry '{industry}' is a target" if fits
        else f"industry '{industry or 'unknown'}' isn't a target"))

    status = prospect.email_status
    if status in EMAIL_SHARE:
        email_share, why = EMAIL_SHARE[status], f"email is {status}"
    elif prospect.email:
        email_share, why = OTHER_EMAIL_SHARE, f"email is {status or 'unverified'}"
    else:
        email_share, why = 0.0, "no email"
    parts.append(ScorePart("email", w["email"] * email_share, w["email"], why))

    signals = company.signals if company else []
    hiring = _has_hiring_signal(prospect.personalization_notes, signals)
    parts.append(ScorePart(
        "hiring", w["hiring"] if hiring else 0, w["hiring"],
        "hiring for a relevant role" if hiring else "no hiring signal"))

    location = company.location if company else ""
    fits = _geography_fits(location, geography)
    parts.append(ScorePart(
        "geography", w["geography"] if fits else 0, w["geography"],
        f"located in {location}" if fits
        else f"location '{location or 'unknown'}' isn't a target"))

    tools = company.tech_stack if company else []
    tech_points = min(w["tech_fit"], len(tools) * w["tech_fit"] / TECH_FIT_FULL_AT)
    parts.append(ScorePart(
        "tech_fit", tech_points, w["tech_fit"],
        f"{len(tools)} tool(s) detected on their site" if tools else "no tools detected"))

    raw = sum(p.points for p in parts)
    total = max(MIN_SCORE, min(MAX_SCORE, round(raw)))
    return ScoreResult(total=total, parts=parts)
