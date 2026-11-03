"""Email finders (Hunter, Prospeo, Apollo): opt-in, capped, and every address
they propose is checked like any other before Harvey uses it."""

import json
import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey.integrations import email_finder as ef
from harvey.integrations import email_finders as finders
from harvey.state import StateManager

REAL_CLIENT = httpx.AsyncClient


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.fixture(autouse=True)
def no_finder_env(monkeypatch):
    for var in ("EMAIL_FINDERS", "HUNTER_API_KEY", "PROSPEO_API_KEY", "APOLLO_API_KEY",
                "PROSPEO_MONTHLY_LOOKUPS", "APOLLO_MONTHLY_LOOKUPS", "HUNTER_MONTHLY_SEARCHES"):
        monkeypatch.delenv(var, raising=False)


def serve(monkeypatch, handler):
    """Route the finders' HTTP calls to ``handler``; returns the request log."""
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(finders.httpx, "AsyncClient",
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(wrapped), **kw))
    return seen


class Env:
    reoon_api_key = "k"
    zerobounce_api_key = ""


# ── configuration ──

def test_finders_are_opt_in_ordered_and_need_a_key(monkeypatch):
    assert finders.configured_finders() == []
    monkeypatch.setenv("PROSPEO_API_KEY", "p")
    monkeypatch.setenv("APOLLO_API_KEY", "a")
    assert finders.configured_finders() == []           # keys alone turn nothing on
    monkeypatch.setenv("EMAIL_FINDERS", "apollo, Hunter ,nope,prospeo,apollo")
    assert finders.configured_finders() == ["apollo", "prospeo"]  # no Hunter key; typo ignored


# ── Hunter ──

@pytest.mark.asyncio
async def test_hunter_found_and_not_found(monkeypatch):
    def handler(request):
        assert request.url.path == "/v2/email-finder"
        assert request.url.params["first_name"] == "Nora"
        if request.url.params["domain"] == "acme.example":
            return httpx.Response(200, json={"data": {
                "email": "Nora.V@acme.example", "score": 91,
                "verification": {"status": "valid"}}})
        return httpx.Response(200, json={"data": {"email": None}})
    serve(monkeypatch, handler)
    found = await finders.find_hunter("Nora", "Vale", "acme.example", "k")
    assert (found.email, found.verified_by_provider, found.confidence) == ("nora.v@acme.example", True, 0.91)
    assert await finders.find_hunter("Nora", "Vale", "beta.example", "k") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,outcome", [(429, "unavailable"), (503, "unavailable"),
                                            (451, None), (401, None)])
async def test_hunter_errors(monkeypatch, status, outcome):
    serve(monkeypatch, lambda r: httpx.Response(status, json={"errors": []}))
    if outcome == "unavailable":
        with pytest.raises(finders.FinderUnavailable):
            await finders.find_hunter("Nora", "Vale", "acme.example", "k")
    else:
        assert await finders.find_hunter("Nora", "Vale", "acme.example", "k") is None


# ── Prospeo ──

@pytest.mark.asyncio
async def test_prospeo_request_and_responses(monkeypatch):
    replies = iter([
        httpx.Response(200, json={"error": False, "person": {"email": {
            "email": "nora@acme.example", "status": "VERIFIED", "revealed": True}}}),
        httpx.Response(400, json={"error": True, "error_code": "NO_MATCH"}),
        httpx.Response(400, json={"error": True, "error_code": "INSUFFICIENT_CREDITS"}),
    ])
    seen = serve(monkeypatch, lambda r: next(replies))
    found = await finders.find_prospeo("Nora", "Vale", "acme.example", "pk")
    assert found.email == "nora@acme.example" and found.verified_by_provider
    req = seen[0]
    assert req.method == "POST" and str(req.url) == "https://api.prospeo.io/enrich-person"
    assert req.headers["X-KEY"] == "pk"
    assert json.loads(req.content) == {"data": {"first_name": "Nora", "last_name": "Vale",
                                                "company_website": "acme.example"}}
    assert await finders.find_prospeo("Nora", "Vale", "acme.example", "pk") is None
    with pytest.raises(finders.FinderUnavailable):
        await finders.find_prospeo("Nora", "Vale", "acme.example", "pk")


# ── Apollo ──

@pytest.mark.asyncio
async def test_apollo_never_asks_for_personal_email_or_phone(monkeypatch):
    replies = iter([
        httpx.Response(200, json={"person": {"email": "nvale@acme.example",
                                             "email_status": "verified", "match_confidence": "high"}}),
        httpx.Response(200, json={"person": {"email": "x@acme.example", "match_confidence": "none"}}),
        httpx.Response(200, json={"person": None}),
        httpx.Response(403, json={"error": "plan"}),
    ])
    seen = serve(monkeypatch, lambda r: next(replies))
    found = await finders.find_apollo("Nora", "Vale", "acme.example", "ak")
    assert found.email == "nvale@acme.example" and found.verified_by_provider
    body = json.loads(seen[0].content)
    assert seen[0].headers["x-api-key"] == "ak"
    assert body["reveal_personal_emails"] is False and body["reveal_phone_number"] is False
    for _ in range(3):
        assert await finders.find_apollo("Nora", "Vale", "acme.example", "ak") is None


# ── caps and fall-through ──

@pytest.mark.asyncio
async def test_monthly_cap_is_counted_per_attempt(monkeypatch, state):
    monkeypatch.setenv("EMAIL_FINDERS", "prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")
    monkeypatch.setenv("PROSPEO_MONTHLY_LOOKUPS", "2")
    seen = serve(monkeypatch, lambda r: httpx.Response(400, json={"error": True, "error_code": "NO_MATCH"}))
    for _ in range(3):
        assert [f async for f in finders.ask_finders("Nora", "Vale", "acme.example", state)] == []
    assert len(seen) == 2
    assert await finders.remaining(state, "prospeo") == 0


@pytest.mark.asyncio
async def test_no_state_means_no_calls(monkeypatch):
    monkeypatch.setenv("EMAIL_FINDERS", "prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")
    seen = serve(monkeypatch, lambda r: httpx.Response(500))
    assert [f async for f in finders.ask_finders("Nora", "Vale", "acme.example", None)] == []
    assert seen == []


@pytest.mark.asyncio
async def test_an_unavailable_finder_falls_through_to_the_next(monkeypatch, state):
    monkeypatch.setenv("EMAIL_FINDERS", "prospeo,apollo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")
    monkeypatch.setenv("APOLLO_API_KEY", "ak")

    def handler(request):
        if request.url.host == "api.prospeo.io":
            return httpx.Response(429)
        return httpx.Response(200, json={"person": {"email": "nora@acme.example",
                                                    "match_confidence": "high"}})
    serve(monkeypatch, handler)
    found = [f async for f in finders.ask_finders("Nora", "Vale", "acme.example", state)]
    assert [(f.provider, f.email) for f in found] == [("apollo", "nora@acme.example")]


# ── find_email: a finder's address is checked like any other ──

@pytest.fixture
def company(monkeypatch):
    """acme.example on Google Workspace; Reoon says the format address doesn't
    exist and grades anything else from ``verdicts``."""
    verdicts = {}

    async def google_mx(domain):
        return "aspmx.l.google.com"

    async def reoon(email, key):
        if email == "nora.vale@acme.example":
            return {"status": "invalid", "catch_all": False}
        return verdicts.get(email)

    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    monkeypatch.setattr(ef, "verify_reoon", reoon)
    monkeypatch.setenv("EMAIL_FINDERS", "prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")
    return verdicts


def prospeo_returns(monkeypatch, email, status="VALID"):
    return serve(monkeypatch, lambda r: httpx.Response(200, json={
        "error": False, "person": {"email": {"email": email, "status": status}}}))


@pytest.mark.asyncio
async def test_finder_address_used_when_the_format_fails(monkeypatch, state, company):
    company["nv@acme.example"] = {"status": "valid", "catch_all": False}
    prospeo_returns(monkeypatch, "nv@acme.example")
    result = await ef.find_email("Nora", "Vale", "acme.example", env=Env(), state=state)
    assert (result.email, result.status, result.source) == ("nv@acme.example", "verified", "finder:prospeo")


@pytest.mark.asyncio
async def test_finders_off_means_no_calls(monkeypatch, state, company):
    monkeypatch.delenv("EMAIL_FINDERS")
    seen = prospeo_returns(monkeypatch, "nv@acme.example")
    result = await ef.find_email("Nora", "Vale", "acme.example", env=Env(), state=state)
    assert result.status == "invalid" and seen == []


@pytest.mark.asyncio
async def test_finders_not_asked_when_the_format_verifies(monkeypatch, state, company):
    async def reoon_ok(email, key):
        return {"status": "valid", "catch_all": False}
    monkeypatch.setattr(ef, "verify_reoon", reoon_ok)
    seen = prospeo_returns(monkeypatch, "nv@acme.example")
    result = await ef.find_email("Nora", "Vale", "acme.example", env=Env(), state=state)
    assert result.email == "nora.vale@acme.example" and result.status == "verified"
    assert seen == []


@pytest.mark.asyncio
async def test_address_on_another_domain_is_never_used(monkeypatch, state, company):
    company["nora.vale@mail.example"] = {"status": "valid", "catch_all": False}
    prospeo_returns(monkeypatch, "nora.vale@mail.example", "VERIFIED")
    result = await ef.find_email("Nora", "Vale", "acme.example", env=Env(), state=state)
    assert result.email == "nora.vale@acme.example" and result.status == "invalid"


@pytest.mark.asyncio
async def test_another_named_persons_mailbox_is_never_used(monkeypatch, state, company):
    company["owen@acme.example"] = {"status": "valid", "catch_all": False}
    prospeo_returns(monkeypatch, "owen@acme.example", "VERIFIED")
    result = await ef.find_email(
        "Nora", "Vale", "acme.example", env=Env(), state=state,
        known_emails=[("owen@acme.example", "Owen", "Pike")])
    # Owen's published address teaches the format (first@), so Nora's own
    # candidate is nora@; the finder's answer, Owen's mailbox, is refused.
    assert result.email == "nora@acme.example"
    assert result.source == "" and not result.sendable


@pytest.mark.asyncio
async def test_our_verifier_wins_over_the_finder(monkeypatch, state, company):
    company["nv@acme.example"] = {"status": "invalid", "catch_all": False}
    prospeo_returns(monkeypatch, "nv@acme.example", "VERIFIED")
    result = await ef.find_email("Nora", "Vale", "acme.example", env=Env(), state=state)
    assert result.email == "nora.vale@acme.example" and result.status == "invalid"


@pytest.mark.asyncio
async def test_finder_verdict_counts_only_when_our_verifiers_cant_tell(monkeypatch, state, company):
    prospeo_returns(monkeypatch, "nv@acme.example", "VERIFIED")   # Reoon: no verdict
    result = await ef.find_email("Nora", "Vale", "acme.example", env=Env(), state=state)
    assert (result.email, result.status) == ("nv@acme.example", "verified")


@pytest.mark.asyncio
async def test_unchecked_finder_address_is_only_a_guess(monkeypatch, state, company):
    prospeo_returns(monkeypatch, "nv@acme.example", "UNVERIFIED")
    result = await ef.find_email("Nora", "Vale", "acme.example", env=Env(), state=state)
    assert (result.email, result.status) == ("nv@acme.example", "guess")
    assert not result.sendable


# ── Company search: other people at the company ──

from types import SimpleNamespace

from harvey.agents.scout import Scout
from harvey.config import ComplianceConfig
from harvey.models.prospect import Prospect


def test_rank_by_titles_keeps_matches_best_first():
    people = [finders.Person("Ida", "Cole", "Office Manager", "x"),
              finders.Person("Ben", "Ross", "Plant Head", "x"),
              finders.Person("Ava", "Lin", "Managing Director", "x"),
              finders.Person("Tom", "Hale", "Plant Head - Unit 2", "x")]
    ranked = finders.rank_by_titles(people, ["Managing Director", "Plant Head"])
    assert [p.first for p in ranked] == ["Ava", "Ben", "Tom"]
    assert [p.first for p in finders.rank_by_titles(people, [])] == ["Ida", "Ben", "Ava", "Tom"]


@pytest.mark.asyncio
async def test_apollo_search_then_lookup_by_id(monkeypatch, state):
    monkeypatch.setenv("EMAIL_FINDERS", "apollo")
    monkeypatch.setenv("APOLLO_API_KEY", "ak")

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/mixed_people/api_search"):
            assert body["q_organization_domains_list"] == ["acme.example"]
            assert body["person_titles"] == ["Managing Director", "Plant Head"]
            return httpx.Response(200, json={"people": [
                {"id": "p1", "first_name": "Ben", "title": "Plant Head", "has_email": True},
                {"id": "p2", "first_name": "Ava", "title": "Managing Director", "has_email": True},
                {"id": "p3", "first_name": "Kai", "title": "Managing Director", "has_email": False},
                {"id": "p4", "first_name": "Ida", "title": "Accountant", "has_email": True}]})
        assert body == {"id": body["id"], "reveal_personal_emails": False, "reveal_phone_number": False}
        names = {"p1": ("Ben", "Ross"), "p2": ("Ava", "Lin")}
        first, last = names[body["id"]]
        return httpx.Response(200, json={"person": {
            "first_name": first, "last_name": last, "title": "x",
            "email": f"{first.lower()}@acme.example", "email_status": "verified"}})
    seen = serve(monkeypatch, handler)
    got = [p async for p in finders.company_contacts(
        "acme.example", ["Managing Director", "Plant Head"], state, want=1)]
    assert [(p.first, p.last, p.email) for p in got] == [("Ava", "Lin", "ava@acme.example"),
                                                        ("Ben", "Ross", "ben@acme.example")]
    # Search is free; each lookup counts against the Apollo cap.
    assert len(seen) == 3 and await finders.remaining(state, "apollo") == 98


@pytest.mark.asyncio
async def test_prospeo_search_skips_people_already_on_file(monkeypatch, state):
    monkeypatch.setenv("EMAIL_FINDERS", "prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")

    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/search-person":
            assert body["filters"]["company"] == {"websites": {"include": ["acme.example"]}}
            return httpx.Response(200, json={"error": False, "results": [
                {"person": {"person_id": "a", "first_name": "Nora", "last_name": "Vale",
                            "current_job_title": "Managing Director"}},
                {"person": {"person_id": "b", "first_name": "Ben", "last_name": "Ross",
                            "current_job_title": "Plant Head"}}]})
        assert body == {"data": {"person_id": "b"}}
        return httpx.Response(200, json={"error": False, "person": {
            "first_name": "Ben", "last_name": "Ross", "current_job_title": "Plant Head",
            "email": {"email": "ben.ross@acme.example", "status": "VERIFIED"}}})
    serve(monkeypatch, handler)
    got = [p async for p in finders.company_contacts(
        "acme.example", ["Managing Director", "Plant Head"], state, want=2,
        skip_names={"nora vale"})]
    assert [(p.first, p.email, p.verified_by_provider) for p in got] == [
        ("Ben", "ben.ross@acme.example", True)]


@pytest.mark.asyncio
async def test_company_search_respects_the_cap(monkeypatch, state):
    monkeypatch.setenv("EMAIL_FINDERS", "prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")
    monkeypatch.setenv("PROSPEO_MONTHLY_LOOKUPS", "1")   # the search uses it up
    seen = serve(monkeypatch, lambda r: httpx.Response(200, json={"error": False, "results": [
        {"person": {"person_id": "b", "first_name": "Ben", "last_name": "Ross",
                    "current_job_title": "Plant Head"}}]}))
    got = [p async for p in finders.company_contacts("acme.example", ["Plant Head"], state, want=2)]
    assert got == [] and len(seen) == 1


def scout_for(state, titles=("Managing Director", "Plant Head"), dnc=()):
    config = SimpleNamespace(
        icp=SimpleNamespace(titles=list(titles), industries=["forging company"], geography=[]),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)),
        compliance=ComplianceConfig(do_not_contact=list(dnc)))
    return Scout(brain=None, state=state, config=config, env=Env())


async def add_company(state, cid="c1", domain="acme.example", checked=True, prospects=()):
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "INSERT INTO companies (id, name, domain, industry, prospects_checked_at) "
            "VALUES (?, 'Acme Forge', ?, 'forging company', ?)",
            (cid, domain, "2026-01-01 00:00:00" if checked else None))
        await db.commit()
    for first, last, email, email_status, status in prospects:
        p = Prospect(first_name=first, last_name=last, email=email, email_status=email_status,
                     company="Acme Forge", company_id=cid, title="Managing Director",
                     source="company_website")
        await state.add_prospect(p)
        if status != "new":
            await state.update_prospect_status(p.id, status)


@pytest.mark.asyncio
async def test_scout_adds_the_best_reachable_colleagues(monkeypatch, state):
    await add_company(state, prospects=[("Nora", "Vale", "nora.vale@acme.example", "invalid", "new")])
    monkeypatch.setenv("EMAIL_FINDERS", "prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")

    def handler(request):
        if request.url.path == "/search-person":
            return httpx.Response(200, json={"error": False, "results": [
                {"person": {"person_id": pid, "first_name": f, "last_name": l, "current_job_title": t}}
                for pid, f, l, t in [("1", "Nora", "Vale", "Managing Director"),
                                     ("2", "Ben", "Ross", "Plant Head"),
                                     ("3", "Ava", "Lin", "Managing Director"),
                                     ("4", "Tom", "Hale", "Plant Head")]]})
        pid = json.loads(request.content)["data"]["person_id"]
        names = {"2": ("Ben", "Ross"), "3": ("Ava", "Lin"), "4": ("Tom", "Hale")}
        f, l = names[pid]
        return httpx.Response(200, json={"error": False, "person": {
            "first_name": f, "last_name": l, "current_job_title": "x",
            "email": {"email": f"{f.lower()}@acme.example", "status": "UNVERIFIED"}}})
    serve(monkeypatch, handler)

    async def google_mx(domain):
        return "aspmx.l.google.com"

    async def reoon(email, key):  # Ava's mailbox is gone; the others exist
        return {"status": "invalid" if email.startswith("ava") else "valid", "catch_all": False}
    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    monkeypatch.setattr(ef, "verify_reoon", reoon)

    added = await scout_for(state)._prospect_via_company_finders()
    assert added == 2
    rows = await state.get_prospects_by_status("new")
    found = sorted((p.first_name, p.email, p.email_status, p.source) for p in rows if p.source != "company_website")
    assert found == [("Ben", "ben@acme.example", "verified", "finder:prospeo"),
                     ("Tom", "tom@acme.example", "verified", "finder:prospeo")]
    # Searched once: not picked again before 90 days.
    assert await state.companies_without_reachable_contact() == []


@pytest.mark.asyncio
async def test_companies_left_alone(monkeypatch, state):
    # Already reachable, already emailed, site not read yet, on do-not-contact.
    await add_company(state, "c1", "one.example",
                      prospects=[("Ann", "One", "ann@one.example", "verified", "new")])
    await add_company(state, "c2", "two.example",
                      prospects=[("Bo", "Two", "bo.two@two.example", "invalid", "contacted")])
    await add_company(state, "c3", "three.example", checked=False)
    await add_company(state, "c4", "blocked.example")
    assert [c["id"] for c in await state.companies_without_reachable_contact()] == ["c4"]

    monkeypatch.setenv("EMAIL_FINDERS", "prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")
    seen = serve(monkeypatch, lambda r: httpx.Response(500))
    assert await scout_for(state, dnc=["blocked.example"])._prospect_via_company_finders() == 0
    assert seen == [] and await state.companies_without_reachable_contact() == []


@pytest.mark.asyncio
async def test_company_search_off_without_finders_or_when_wanted_is_zero(monkeypatch, state):
    await add_company(state)
    assert await scout_for(state)._prospect_via_company_finders() == 0
    monkeypatch.setenv("EMAIL_FINDERS", "prospeo")
    monkeypatch.setenv("PROSPEO_API_KEY", "pk")
    monkeypatch.setenv("EMAIL_FINDER_COMPANY_CONTACTS", "0")
    seen = serve(monkeypatch, lambda r: httpx.Response(500))
    assert await scout_for(state)._prospect_via_company_finders() == 0 and seen == []


# ── Which domains count as the company's own ──

@pytest.mark.parametrize("email,domain,ok", [
    ("a@acme.example", "acme.example", True),
    ("a@mail.acme.example", "acme.example", True),          # subdomain
    ("a@acme.example", "in.acme.example", True),            # parent of a regional site
    ("a@acme.co.uk", "uk.acme.co.uk", True),                # parent under a second-level suffix
    ("a@co.uk", "acme.co.uk", False),                       # never a public suffix
    ("a@other.example", "in.acme.example", False),          # a sibling company
    ("a@group.example", "acme.example", False),             # a group or holding domain
    ("a@godaddysites.com", "acme.godaddysites.com", False), # a shared host's parent
    ("a@notacme.example", "acme.example", False),
    ("", "acme.example", False),
])
def test_on_company_domain(email, domain, ok):
    assert ef.on_company_domain(email, domain) is ok


@pytest.mark.asyncio
async def test_parent_domain_address_is_graded_on_its_own_mail_server(monkeypatch, state, company):
    looked_up = []

    async def mx(domain):
        looked_up.append(domain)
        return "aspmx.l.google.com"
    monkeypatch.setattr(ef, "get_mx_host", mx)
    company["nora@acme.example"] = {"status": "valid", "catch_all": False}
    prospeo_returns(monkeypatch, "nora@acme.example")
    result = await ef.find_email("Nora", "Vale", "in.acme.example", env=Env(), state=state)
    assert (result.email, result.status) == ("nora@acme.example", "verified")
    assert looked_up == ["in.acme.example", "acme.example"]
