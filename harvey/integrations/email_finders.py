"""Email finders: ask a contact-data service for one named person's address.

The pattern pipeline in email_finder.py builds an address from the company's
format and verifies it. That fails for the people who matter most at many
companies: an owner or director whose mailbox doesn't follow the format
(``md@``, ``sid@``, a group domain). A finder answers the narrower question
"what is Nora Vale's address at acme.example?" from the addresses the service
has on file for that person.

Finders are opt-in and ordered: ``EMAIL_FINDERS=prospeo,hunter,apollo`` in
.env, each with its own key. A provider without a key is skipped. Each has a
monthly cap (Hunter's is HUNTER_MONTHLY_SEARCHES, shared with its domain
search), counted per attempt, so a run can never spend more than the user allowed.
None of these services charge when they find nobody.

A finder only proposes. Its address is used only if it's at the company's own
domain, isn't another named person's mailbox, and then passes the same
verifiers as any other candidate (see email_finder.find_email).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("harvey.email_finders")

API_TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)

# Default monthly attempts per provider when no cap is set in .env. Small on
# purpose: matches the free tiers, so turning a finder on can't drain credits.
# Hunter isn't here: its finder draws on HUNTER_MONTHLY_SEARCHES, the budget
# its domain search already counts against (see email_finder).
DEFAULT_MONTHLY_CAPS = {"prospeo": 75, "apollo": 100}
CAP_ENV = {"prospeo": "PROSPEO_MONTHLY_LOOKUPS", "apollo": "APOLLO_MONTHLY_LOOKUPS"}
KEY_ENV = {"hunter": "HUNTER_API_KEY", "prospeo": "PROSPEO_API_KEY", "apollo": "APOLLO_API_KEY"}
KNOWN = tuple(KEY_ENV)


class FinderUnavailable(Exception):
    """The service couldn't answer now (timeout, rate limit, 5xx, no credits).
    Transient: the person may be looked up again later."""


@dataclass
class Found:
    """One address a finder proposes, with the service's own opinion of it."""
    email: str
    provider: str
    verified_by_provider: bool = False   # the service says it checked the mailbox
    confidence: float = 0.0              # 0-1, as reported (0 when not given)


# ── Configuration ──

def configured_finders() -> list[str]:
    """Providers named in EMAIL_FINDERS that have a key, in the given order.

    Unknown names are ignored with a warning, so a typo turns one provider
    off rather than stopping Harvey."""
    raw = os.getenv("EMAIL_FINDERS", "")
    out = []
    for name in (p.strip().lower() for p in raw.split(",")):
        if not name:
            continue
        if name not in KNOWN:
            logger.warning(f"EMAIL_FINDERS: unknown provider '{name}' ignored "
                           f"(known: {', '.join(KNOWN)})")
            continue
        if name not in out and os.getenv(KEY_ENV[name], "").strip():
            out.append(name)
    return out


def monthly_cap(provider: str) -> int:
    raw = os.getenv(CAP_ENV[provider], "").strip()
    try:
        return max(int(raw), 0) if raw else DEFAULT_MONTHLY_CAPS[provider]
    except ValueError:
        return DEFAULT_MONTHLY_CAPS[provider]


def usage_key(provider: str) -> str:
    return f"email_finder_{provider}_count:{datetime.now(timezone.utc):%Y-%m}"


async def remaining(state, provider: str) -> int:
    """Attempts left this month. Without state there is no ledger, so none."""
    if state is None:
        return 0
    if provider == "hunter":
        from harvey.integrations.email_finder import hunter_searches_remaining
        return await hunter_searches_remaining(state)
    try:
        used = int(await state.get_setting(usage_key(provider)) or 0)
    except Exception as e:
        logger.debug(f"Finder usage read failed for {provider}: {e}")
        return 0
    return max(monthly_cap(provider) - used, 0)


async def record_attempt(state, provider: str) -> None:
    if provider == "hunter":
        from harvey.integrations.email_finder import record_hunter_search
        await record_hunter_search(state)
        return
    try:
        await state.increment_setting(usage_key(provider))
    except Exception as e:
        logger.debug(f"Finder usage write failed for {provider}: {e}")


# ── Providers ──
# Each returns a Found, None when the service has no address for the person,
# or raises FinderUnavailable when it couldn't answer.

def _transient(resp: httpx.Response) -> bool:
    return resp.status_code == 429 or resp.status_code >= 500


async def find_hunter(first: str, last: str, domain: str, key: str) -> Optional[Found]:
    """Hunter Email Finder: GET /v2/email-finder. No credit when not found."""
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/email-finder",
                params={"domain": domain, "first_name": first, "last_name": last,
                        "api_key": key},
            )
    except httpx.HTTPError as e:
        raise FinderUnavailable(str(e)) from e
    if _transient(resp):
        raise FinderUnavailable(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        # 400 bad input, 401 key, 451 the person asked Hunter to stop: no address.
        return None
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        raise FinderUnavailable("bad JSON")
    email = data.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None
    status = str(((data.get("verification") or {}).get("status")) or "").lower()
    score = data.get("score")
    return Found(email=email.strip().lower(), provider="hunter",
                 verified_by_provider=status == "valid",
                 confidence=(score / 100) if isinstance(score, (int, float)) else 0.0)


async def find_prospeo(first: str, last: str, domain: str, key: str) -> Optional[Found]:
    """Prospeo Enrich Person: POST /enrich-person. No credit when no match."""
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.post(
                "https://api.prospeo.io/enrich-person",
                headers={"X-KEY": key, "Content-Type": "application/json"},
                json={"data": {"first_name": first, "last_name": last,
                               "company_website": domain}},
            )
    except httpx.HTTPError as e:
        raise FinderUnavailable(str(e)) from e
    if _transient(resp):
        raise FinderUnavailable(f"HTTP {resp.status_code}")
    try:
        body = resp.json() or {}
    except ValueError:
        raise FinderUnavailable("bad JSON")
    if not isinstance(body, dict):
        raise FinderUnavailable("unexpected response")
    if body.get("error") or resp.status_code != 200:
        code = str(body.get("error_code") or "")
        if code == "INSUFFICIENT_CREDITS":
            raise FinderUnavailable(code)
        return None  # NO_MATCH, INVALID_DATAPOINTS, INVALID_API_KEY, ...
    email_obj = ((body.get("person") or {}).get("email")) or {}
    email = email_obj.get("email") if isinstance(email_obj, dict) else None
    if not isinstance(email, str) or "@" not in email:
        return None
    status = str(email_obj.get("status") or "").upper()
    return Found(email=email.strip().lower(), provider="prospeo",
                 verified_by_provider=status == "VERIFIED")


async def find_apollo(first: str, last: str, domain: str, key: str) -> Optional[Found]:
    """Apollo People Enrichment: POST /api/v1/people/match. No credit when
    nothing is found. Never asks for personal emails or phone numbers."""
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.post(
                "https://api.apollo.io/api/v1/people/match",
                headers={"x-api-key": key, "Content-Type": "application/json",
                         "Cache-Control": "no-cache"},
                json={"first_name": first, "last_name": last, "domain": domain,
                      "reveal_personal_emails": False, "reveal_phone_number": False},
            )
    except httpx.HTTPError as e:
        raise FinderUnavailable(str(e)) from e
    if _transient(resp):
        raise FinderUnavailable(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        return None  # 401/403: key or plan without API access
    try:
        body = resp.json() or {}
    except ValueError:
        raise FinderUnavailable("bad JSON")
    person = body.get("person") if isinstance(body, dict) else None
    if not isinstance(person, dict):
        return None
    if str(person.get("match_confidence") or "").lower() == "none":
        return None
    email = person.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None
    return Found(email=email.strip().lower(), provider="apollo",
                 verified_by_provider=str(person.get("email_status") or "").lower() == "verified")


PROVIDERS: dict[str, Callable[[str, str, str, str], Awaitable[Optional[Found]]]] = {
    "hunter": find_hunter,
    "prospeo": find_prospeo,
    "apollo": find_apollo,
}


async def ask_finders(first: str, last: str, domain: str, state):
    """Yield each configured finder's proposal, in order, within each cap.

    An async generator so the caller can stop at the first address that
    verifies and spend nothing on the providers after it."""
    for name in configured_finders():
        if await remaining(state, name) <= 0:
            logger.info(f"Email finder {name}: monthly cap reached, skipped")
            continue
        await record_attempt(state, name)
        try:
            found = await PROVIDERS[name](first, last, domain, os.getenv(KEY_ENV[name], "").strip())
        except FinderUnavailable as e:
            logger.info(f"Email finder {name} unavailable for {domain}: {e}")
            continue
        except Exception as e:  # a provider bug must not stop prospecting
            logger.warning(f"Email finder {name} failed for {domain}: {e}")
            continue
        if found is not None:
            yield found


# ── Company search: the people a finder has on file at a domain ──
# Used when a company's own site gave no one Harvey can reach. A search
# lists people (with titles) at the domain; the best matches for the ICP's
# titles then get their address looked up, one at a time, so credits go
# only to people Harvey would actually write to.

@dataclass
class Person:
    """Someone a finder lists at a company. ``email`` is filled by the search
    itself (Hunter) or by a lookup on ``ref`` (Apollo, Prospeo)."""
    first: str
    last: str
    title: str
    provider: str
    ref: str = ""
    email: str = ""
    verified_by_provider: bool = False


def company_contacts_wanted() -> int:
    """How many reachable contacts to add per company (0 turns the company
    search off while keeping person lookups)."""
    raw = os.getenv("EMAIL_FINDER_COMPANY_CONTACTS", "").strip()
    try:
        return max(int(raw), 0) if raw else 2
    except ValueError:
        return 2


async def people_hunter(domain: str, titles: list[str], key: str) -> list[Person]:
    """Hunter Domain Search: people with their address and Hunter's verdict."""
    from harvey.integrations.email_finder import hunter_domain_people
    out = []
    for p in await hunter_domain_people(domain, key) or []:
        if not isinstance(p, dict) or not isinstance(p.get("value"), str):
            continue
        status = str(((p.get("verification") or {}).get("status")) or "").lower()
        out.append(Person(first=p.get("first_name") or "", last=p.get("last_name") or "",
                          title=p.get("position") or "", provider="hunter",
                          email=p["value"].strip().lower(),
                          verified_by_provider=status == "valid"))
    return out


async def people_apollo(domain: str, titles: list[str], key: str) -> list[Person]:
    """Apollo People API Search: no credits, no addresses (looked up by id)."""
    body = {"q_organization_domains_list": [domain], "per_page": 25}
    if titles:
        body["person_titles"] = titles
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.post(
                "https://api.apollo.io/api/v1/mixed_people/api_search",
                headers={"x-api-key": key, "Content-Type": "application/json",
                         "Cache-Control": "no-cache"},
                json=body,
            )
    except httpx.HTTPError as e:
        raise FinderUnavailable(str(e)) from e
    if _transient(resp):
        raise FinderUnavailable(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        return []
    try:
        people = (resp.json() or {}).get("people") or []
    except ValueError:
        raise FinderUnavailable("bad JSON")
    return [Person(first=p.get("first_name") or "", last="", title=p.get("title") or "",
                   provider="apollo", ref=str(p.get("id") or ""))
            for p in people if isinstance(p, dict) and p.get("id")
            and p.get("has_email") is not False]


async def people_prospeo(domain: str, titles: list[str], key: str) -> list[Person]:
    """Prospeo Search Person: one credit when it lists anyone; no addresses."""
    filters = {"company": {"websites": {"include": [domain]}}}
    if titles:
        filters["person_job_title"] = {"include": titles, "match_mode": "CONTAINS"}
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.post(
                "https://api.prospeo.io/search-person",
                headers={"X-KEY": key, "Content-Type": "application/json"},
                json={"filters": filters, "page": 1},
            )
    except httpx.HTTPError as e:
        raise FinderUnavailable(str(e)) from e
    if _transient(resp):
        raise FinderUnavailable(f"HTTP {resp.status_code}")
    try:
        body = resp.json() or {}
    except ValueError:
        raise FinderUnavailable("bad JSON")
    if not isinstance(body, dict) or body.get("error") or resp.status_code != 200:
        if isinstance(body, dict) and body.get("error_code") == "INSUFFICIENT_CREDITS":
            raise FinderUnavailable("INSUFFICIENT_CREDITS")
        return []
    out = []
    for row in body.get("results") or []:
        p = (row or {}).get("person") if isinstance(row, dict) else None
        if isinstance(p, dict) and p.get("person_id"):
            out.append(Person(first=p.get("first_name") or "", last=p.get("last_name") or "",
                              title=p.get("current_job_title") or "", provider="prospeo",
                              ref=str(p["person_id"])))
    return out


async def reveal_apollo(person: Person, key: str) -> Optional[Person]:
    """The address (and full name) for an Apollo search result, by its id."""
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.post(
                "https://api.apollo.io/api/v1/people/match",
                headers={"x-api-key": key, "Content-Type": "application/json",
                         "Cache-Control": "no-cache"},
                json={"id": person.ref, "reveal_personal_emails": False,
                      "reveal_phone_number": False},
            )
    except httpx.HTTPError as e:
        raise FinderUnavailable(str(e)) from e
    if _transient(resp):
        raise FinderUnavailable(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        return None
    try:
        p = (resp.json() or {}).get("person")
    except ValueError:
        raise FinderUnavailable("bad JSON")
    if not isinstance(p, dict) or not isinstance(p.get("email"), str) or "@" not in p["email"]:
        return None
    return Person(first=p.get("first_name") or person.first, last=p.get("last_name") or "",
                  title=p.get("title") or person.title, provider="apollo", ref=person.ref,
                  email=p["email"].strip().lower(),
                  verified_by_provider=str(p.get("email_status") or "").lower() == "verified")


async def reveal_prospeo(person: Person, key: str) -> Optional[Person]:
    """The address for a Prospeo search result, by its person_id."""
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.post(
                "https://api.prospeo.io/enrich-person",
                headers={"X-KEY": key, "Content-Type": "application/json"},
                json={"data": {"person_id": person.ref}},
            )
    except httpx.HTTPError as e:
        raise FinderUnavailable(str(e)) from e
    if _transient(resp):
        raise FinderUnavailable(f"HTTP {resp.status_code}")
    try:
        body = resp.json() or {}
    except ValueError:
        raise FinderUnavailable("bad JSON")
    if not isinstance(body, dict) or body.get("error") or resp.status_code != 200:
        if isinstance(body, dict) and body.get("error_code") == "INSUFFICIENT_CREDITS":
            raise FinderUnavailable("INSUFFICIENT_CREDITS")
        return None
    p = body.get("person") or {}
    email_obj = p.get("email") if isinstance(p, dict) else None
    email = email_obj.get("email") if isinstance(email_obj, dict) else None
    if not isinstance(email, str) or "@" not in email:
        return None
    return Person(first=p.get("first_name") or person.first, last=p.get("last_name") or person.last,
                  title=p.get("current_job_title") or person.title, provider="prospeo",
                  ref=person.ref, email=email.strip().lower(),
                  verified_by_provider=str(email_obj.get("status") or "").upper() == "VERIFIED")


SEARCHES = {"hunter": people_hunter, "apollo": people_apollo, "prospeo": people_prospeo}
REVEALS = {"apollo": reveal_apollo, "prospeo": reveal_prospeo}
# Whether a provider's search itself costs a credit (Apollo's search is free).
SEARCH_COSTS = {"hunter": True, "prospeo": True, "apollo": False}


def rank_by_titles(people: list[Person], titles: list[str]) -> list[Person]:
    """People whose title matches the ICP, best first: a title earlier in
    ``icp.titles`` ranks higher. With no titles configured, keep the order."""
    if not titles:
        return list(people)
    from harvey.collectors.people import title_matches
    ranked = []
    for i, p in enumerate(people):
        rank = next((n for n, t in enumerate(titles) if title_matches(p.title, [t])), None)
        if rank is not None:
            ranked.append((rank, i, p))
    return [p for _, _, p in sorted(ranked, key=lambda x: (x[0], x[1]))]


async def company_contacts(domain: str, titles: list[str], state, want: int,
                           skip_names: set[str] = frozenset()):
    """Yield people at ``domain`` whose title fits, best first, each with an
    address to check. Providers are asked in EMAIL_FINDERS order; credits are
    spent only within each provider's monthly cap, and an address is looked
    up for at most ``want * 2`` people per provider."""
    for name in configured_finders():
        key = os.getenv(KEY_ENV[name], "").strip()
        if SEARCH_COSTS[name]:
            if await remaining(state, name) <= 0:
                logger.info(f"Email finder {name}: monthly cap reached, company search skipped")
                continue
            await record_attempt(state, name)
        try:
            people = await SEARCHES[name](domain, titles, key)
        except FinderUnavailable as e:
            logger.info(f"Email finder {name} company search unavailable for {domain}: {e}")
            continue
        except Exception as e:
            logger.warning(f"Email finder {name} company search failed for {domain}: {e}")
            continue
        picked = [p for p in rank_by_titles(people, titles)
                  if f"{p.first} {p.last}".strip().lower() not in skip_names][: max(want, 1) * 2]
        for person in picked:
            if not person.email and name in REVEALS:
                if await remaining(state, name) <= 0:
                    logger.info(f"Email finder {name}: monthly cap reached mid-company")
                    break
                await record_attempt(state, name)
                try:
                    person = await REVEALS[name](person, key)
                except FinderUnavailable as e:
                    logger.info(f"Email finder {name} lookup unavailable for {domain}: {e}")
                    break
                except Exception as e:
                    logger.warning(f"Email finder {name} lookup failed for {domain}: {e}")
                    continue
                if person is None:
                    continue
            if person.email and f"{person.first} {person.last}".strip().lower() not in skip_names:
                yield person
