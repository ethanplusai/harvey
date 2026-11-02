"""REPUTATION — Google reviews read for businesses found on Google Maps.

A paid stage, so the tests care about the same things discovery's do: an
honest estimate, a cap enforced before spending, nothing bought without a
confirmed signal, and no fact recorded from an answer that doesn't support
it. And one thing of its own: no review text or reviewer ever lands in the
database.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.collectors import reviews as R
from harvey.config import EnvConfig
from harvey.models import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for code in R.REVIEW_CODES:
            await sm.set_signal_status(code, "confirmed")
        yield sm


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test waits out Scavio's one-request-a-second spacing."""
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr(D.asyncio, "sleep", no_sleep)
    monkeypatch.setattr("harvey.config.load_env", lambda: EnvConfig(scavio_api_key="fixture"))


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _review(days_ago, rating=5, answered=True, edited_days_ago=None):
    item = {"rating": rating, "iso_date": _iso(days_ago),
            "iso_date_of_last_edit": _iso(days_ago if edited_days_ago is None else edited_days_ago),
            "snippet": "Fixture review text that must never be stored.",
            "user": {"name": "Fixture Reviewer", "link": "https://example.com/reviewer"}}
    if answered:
        item["response"] = {"snippet": "Thanks from the owner."}
    return item


def _page(reviews, total=None, more=False, topics=None):
    body = {"reviews": reviews, "credits_used": 1,
            "place_info": {"title": "Acme Roofing", "reviews": total if total is not None else len(reviews)},
            "search_metadata": {"google_maps_url": "https://www.google.com/maps/place/?q=place_id:ChIJfixture"},
            "topics": topics or []}
    if more:
        body["pagination"] = {"next_page_token": "fixture-token"}
    return body


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _use(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(R.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))


async def _company(state, name, external_id, domain=""):
    return await state.add_company(Company(name=name, domain=domain, external_id=external_id))


# ── Which businesses can be read ──

@pytest.mark.parametrize("external_id, ref", [
    ("gmaps:ChIJabc123", ("place_id", "ChIJabc123")),
    ("dataforseo:ChIJabc123", ("place_id", "ChIJabc123")),
    ("dataforseo:cid:1234567890", ("data_cid", "1234567890")),
    ("dataforseo:cid:not-a-number", None),
    ("osm:node/123", None),
    ("", None),
    ("gmaps:", None),
])
def test_place_ref_only_for_google_listings(external_id, ref):
    assert R.place_ref(external_id) == ref


# ── The adapter ──

@pytest.mark.asyncio
async def test_scavio_reads_the_newest_page_by_place_id():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization"),
                     json.loads(request.content)))
        return httpx.Response(200, json=_page([_review(3), _review(40, answered=False)],
                                              total=2, topics=[{"keyword": "roof repair", "mentions": 12}]))

    provider = R.ScavioReviews()
    async with _client(handler) as client:
        sample = await provider.read(client, {"scavio_api_key": "k"}, ("place_id", "ChIJabc123"))

    url, auth, body = seen[0]
    assert url == "https://api.scavio.dev/api/v2/google/maps/reviews"
    assert auth == "Bearer k"
    assert body == {"place_id": "ChIJabc123", "sort_by": "newest", "num": 20}
    assert provider.last_cost == pytest.approx(0.01)
    assert [r.answered for r in sample.reviews] == [True, False]
    assert sample.total == 2 and sample.complete
    assert sample.topics == [("roof repair", 12)]


@pytest.mark.asyncio
async def test_a_dataforseo_cid_is_looked_up_first_and_costs_two_credits():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/maps/place"):
            return httpx.Response(200, json={"place_results": {
                "place_id": "ChIJabc123", "data_id": "0x1:0x2"}, "credits_used": 1})
        return httpx.Response(200, json=_page([_review(3)], total=1))

    provider = R.ScavioReviews()
    async with _client(handler) as client:
        sample = await provider.read(client, {"scavio_api_key": "k"}, ("data_cid", "123"))

    assert seen[0] == ("/api/v2/google/maps/place", {"data_cid": "123"})
    assert seen[1] == ("/api/v2/google/maps/reviews",
                       {"data_id": "0x1:0x2", "sort_by": "newest", "num": 20})
    assert provider.last_cost == pytest.approx(0.02)
    assert sample.complete
    assert provider.estimate([("data_cid", "1"), ("place_id", "x")]) == pytest.approx(0.03)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"reviews": {}}, {"reviews": ["x"]}, {"error": "fixture"},
                                  {"reviews": [], "pagination": "x"}])
async def test_malformed_paid_responses_are_failures_with_cost(body):
    provider = R.ScavioReviews()
    async with _client(lambda r: httpx.Response(200, json=body)) as client:
        with pytest.raises(RuntimeError, match="Scavio: invalid"):
            await provider.read(client, {"scavio_api_key": "k"}, ("place_id", "ChIJabc"))
    assert provider.last_cost == pytest.approx(0.01)


def test_an_edit_dates_a_review_not_its_first_post():
    """Google's newest sort and its "Edited 2 days ago" label follow the
    edit; a 1-star posted years ago and edited this week is recent."""
    sample = R.ScavioReviews._sample(_page([_review(900, rating=1, edited_days_ago=2)], total=1))
    assert (NOW - sample.reviews[0].when).days == 2


# ── Short answers ──

def test_a_short_page_from_a_bigger_listing_is_not_complete():
    """Google sometimes sends a listing's five-review overview instead of
    the newest twenty. Read as the newest, a busy business looks quiet."""
    short = R.ReviewSample(reviews=[R.Review(5, NOW, True)] * 5, total=400)
    assert not short.complete
    assert R.review_observations(short, "c1", set(R.REVIEW_CODES), "run", now=NOW) == []
    small = R.ReviewSample(reviews=[R.Review(5, NOW, True)] * 5, total=5)
    assert small.complete
    paged = R.ReviewSample(reviews=[R.Review(5, NOW, True)] * 5, total=None, more=True)
    assert paged.complete


# ── What gets recorded ──

def _rows(reviews, **kw):
    sample = R.ScavioReviews._sample(_page(reviews, **kw))
    rows = R.review_observations(sample, "c1", set(R.REVIEW_CODES), "run", now=NOW)
    return {r["signal_code"]: r for r in rows}


def test_reply_rate_recency_and_a_bad_review():
    reviews = ([_review(d) for d in (2, 10, 30, 60)]
               + [_review(45, rating=1, answered=False)]
               + [_review(d, answered=False) for d in (120, 200, 300)])
    rows = _rows(reviews, total=8, topics=[{"keyword": "a", "mentions": 3},
                                           {"keyword": "b", "mentions": 9}])
    assert rows["REVIEW_REPLY_RATE"]["value_num"] == 50.0
    assert rows["REVIEW_REPLY_RATE"]["value_text"] == "4 of the 8 newest"
    assert rows["LAST_REVIEW_DAYS"]["value_num"] == 2
    assert rows["NEW_REVIEWS_90D"]["value_num"] == 5
    assert rows["RECENT_BAD_REVIEW"]["value_num"] == 1
    assert rows["RECENT_BAD_REVIEW"]["value_text"] == (
        f"1-star, {(NOW - timedelta(days=45)).date().isoformat()}, no reply")
    assert rows["REVIEW_TOPICS"]["value_text"] == "b (9); a (3)"
    assert all(r["collector"] == "reviews" for r in rows.values())


def test_no_bad_review_is_recorded_as_none_among_those_read():
    rows = _rows([_review(d) for d in (5, 300)] + [_review(400, rating=1)], total=3)
    assert rows["RECENT_BAD_REVIEW"]["value_num"] == 0
    assert rows["RECENT_BAD_REVIEW"]["value_text"] == "none in the 3 newest"


def test_a_full_page_of_recent_reviews_is_a_floor():
    rows = _rows([_review(d) for d in range(20)], total=900, more=True)
    assert rows["NEW_REVIEWS_90D"]["value_num"] == 20
    assert rows["NEW_REVIEWS_90D"]["value_text"] == "at least 20"


def test_too_few_reviews_give_no_reply_rate():
    rows = _rows([_review(5), _review(9)], total=2)
    assert "REVIEW_REPLY_RATE" not in rows
    assert rows["LAST_REVIEW_DAYS"]["value_num"] == 5


def test_only_confirmed_signals_are_recorded():
    sample = R.ScavioReviews._sample(_page([_review(d) for d in range(6)], total=6))
    rows = R.review_observations(sample, "c1", {"LAST_REVIEW_DAYS"}, "run", now=NOW)
    assert [r["signal_code"] for r in rows] == ["LAST_REVIEW_DAYS"]


def test_no_review_text_or_reviewer_is_ever_recorded():
    rows = _rows([_review(d, rating=1, answered=False) for d in range(6)], total=6)
    stored = json.dumps(list(rows.values()))
    assert "Fixture review text" not in stored
    assert "Fixture Reviewer" not in stored and "reviewer" not in stored


# ── The run ──

@pytest.mark.asyncio
async def test_run_reads_maps_businesses_and_skips_the_rest(state, monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_page([_review(d) for d in (1, 2, 3, 4, 5)], total=5))

    _use(monkeypatch, handler)
    a = await _company(state, "Acme Roofing", "gmaps:ChIJacme", domain="acme-roofing.example")
    await _company(state, "Organic Only", "", domain="organic.example")
    await _company(state, "Mapped Elsewhere", "osm:node/1")

    report = await R.run_reviews(state, limit=10, max_spend=1.0)
    assert report.companies == 1 and report.read == 1
    assert calls == [{"place_id": "ChIJacme", "sort_by": "newest", "num": 20}]
    assert report.actual_cost == pytest.approx(0.01)
    codes = {o["signal_code"] for o in await state.get_observations(company_id=a)}
    assert {"REVIEW_REPLY_RATE", "LAST_REVIEW_DAYS"} <= codes

    # Read once, then left alone until it goes stale.
    again = await R.run_reviews(state, limit=10, max_spend=1.0)
    assert again.companies == 0 and len(calls) == 1


@pytest.mark.asyncio
async def test_estimate_spends_nothing(state, monkeypatch):
    _use(monkeypatch, lambda r: pytest.fail("an estimate must not call the API"))
    await _company(state, "Acme Roofing", "gmaps:ChIJacme")
    await _company(state, "Beta Plumbing", "dataforseo:cid:42")
    report = await R.run_reviews(state, dry_run=True)
    assert report.companies == 2
    assert report.estimated_cost == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_over_the_cap_spends_nothing(state, monkeypatch):
    _use(monkeypatch, lambda r: pytest.fail("over the cap must not call the API"))
    for i in range(5):
        await _company(state, f"Roofer {i}", f"gmaps:ChIJ{i}")
    report = await R.run_reviews(state, max_spend=0.03)
    assert report.stopped == "over budget" and report.actual_cost == 0


@pytest.mark.asyncio
async def test_nothing_is_bought_without_a_confirmed_reputation_signal(state, monkeypatch):
    _use(monkeypatch, lambda r: pytest.fail("no confirmed signal, no purchase"))
    for code in R.REVIEW_CODES:
        await state.set_signal_status(code, "proposed")
    await _company(state, "Acme Roofing", "gmaps:ChIJacme")
    report = await R.run_reviews(state)
    assert report.stopped == "no confirmed signals"


@pytest.mark.asyncio
async def test_missing_key_spends_nothing(state, monkeypatch):
    _use(monkeypatch, lambda r: pytest.fail("no key, no call"))
    monkeypatch.setattr("harvey.config.load_env", lambda: EnvConfig(scavio_api_key=""))
    await _company(state, "Acme Roofing", "gmaps:ChIJacme")
    report = await R.run_reviews(state)
    assert report.stopped == "not configured"
    assert "SCAVIO_API_KEY" in report.errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 429])
async def test_account_errors_stop_the_run(state, monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "fixture_secret_should_not_be_logged"})

    _use(monkeypatch, handler)
    for i in range(3):
        await _company(state, f"Roofer {i}", f"gmaps:ChIJ{i}")
    report = await R.run_reviews(state)
    assert len(calls) == 1 and str(status) in report.stopped
    assert "fixture_secret" not in str(report.as_dict())
    # Nothing was paid for, so nobody is marked read.
    assert len(await state.companies_needing_reviews()) == 3


@pytest.mark.asyncio
async def test_a_short_answer_is_paid_for_recorded_as_nothing_and_rotated(state, monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(
        200, json=_page([_review(d) for d in range(5)], total=400)))
    a = await _company(state, "Acme Roofing", "gmaps:ChIJacme")
    report = await R.run_reviews(state)
    assert report.partial == 1 and report.read == 0 and report.observations == 0
    assert report.actual_cost == pytest.approx(0.01)
    assert await state.get_observations(company_id=a) == []
    assert await state.companies_needing_reviews() == []


@pytest.mark.asyncio
async def test_a_server_error_is_unpaid_and_retried_next_run(state, monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(502, json={}))
    await _company(state, "Acme Roofing", "gmaps:ChIJacme")
    report = await R.run_reviews(state)
    assert report.failed == 1 and report.actual_cost == 0
    assert len(await state.companies_needing_reviews()) == 1


@pytest.mark.asyncio
async def test_the_discovery_kill_switch_stops_review_reads(state, monkeypatch):
    _use(monkeypatch, lambda r: pytest.fail("paused runs must not call the API"))
    await state.set_setting("discovery_paused", "stopped from dashboard")
    await _company(state, "Acme Roofing", "gmaps:ChIJacme")
    report = await R.run_reviews(state)
    assert report.stopped == "paused"


# ── The writer's facts ──

def _read_at(sample, company_id, when):
    """Observation rows as a run on ``when`` would have stored them."""
    rows = R.review_observations(sample, company_id, set(R.REVIEW_CODES), "run", now=when)
    for row in rows:
        row["observed_at"] = when.strftime("%Y-%m-%d %H:%M:%S")
    return rows


@pytest.mark.asyncio
async def test_review_facts_age_to_today(state):
    a = await _company(state, "Acme Roofing", "gmaps:ChIJacme")
    sample = R.ScavioReviews._sample(_page(
        [_review(d, answered=d < 50) for d in (40, 60, 70, 80, 90)]
        + [_review(30, rating=2, answered=False)], total=6,
        topics=[{"keyword": "insurance claims", "mentions": 125}]))
    seen = NOW - timedelta(days=10)
    await state.add_observations(_read_at(sample, a, seen))
    facts = await R.review_facts(state, a, now=NOW)
    text = "\n".join(facts)
    assert "replied to 1 of the 6 newest reviews" in text
    # The newest was 20 days old when read ten days ago: 30 days now, not 20.
    assert "newest Google review: 4 weeks ago" in text
    assert "A 2-star Google review from 4 weeks ago has no reply from the business" in text
    assert "insurance claims (125)" in text


@pytest.mark.asyncio
async def test_stale_or_absent_readings_give_no_facts(state):
    a = await _company(state, "Acme Roofing", "gmaps:ChIJacme")
    assert await R.review_facts(state, a, now=NOW) == []
    sample = R.ScavioReviews._sample(_page([_review(d) for d in range(6)], total=6))
    old = NOW - timedelta(days=R.STALE_FACT_DAYS + 5)
    await state.add_observations(_read_at(sample, a, old))
    assert await R.review_facts(state, a, now=NOW) == []
    assert await R.review_facts(None, a) == []


def test_reputation_signals_never_read_as_free():
    """`harvey signals --confirm free` must not switch on paid collection."""
    rows = [s for s in SIGNAL_CATALOG if s["code"] in R.REVIEW_CODES]
    assert len(rows) == len(R.REVIEW_CODES)
    for sig in rows:
        assert sig["category"] == "reputation" and sig["collector"] == "reviews"
        assert not sig["cost_note"].lower().startswith(("free", "included"))
        assert "$" in sig["cost_note"]


class RecordingBrain:
    def __init__(self):
        self.prompts = []

    def load_prompt(self, name, **kw):
        return "PROMPT"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"subject": "hi", "body": "Hi"}


@pytest.mark.asyncio
async def test_the_writer_gets_review_facts(state):
    from types import SimpleNamespace

    from harvey.agents.writer import Writer
    from harvey.models.prospect import Prospect

    cid = await _company(state, "Acme Roofing", "gmaps:ChIJacme", domain="acme-roofing.example")
    await state.add_observation("REVIEW_REPLY_RATE", company_id=cid, collector="reviews",
                                value_num=10.0, value_text="2 of the 20 newest")
    await state.add_observation("LAST_REVIEW_DAYS", company_id=cid, collector="reviews",
                                value_num=150.0, value_text="2026-06-05")
    pid = await state.add_prospect(Prospect(
        first_name="Ana", last_name="Doe", title="Owner", company="Acme Roofing",
        company_id=cid, email="ana@acme-roofing.example", email_status="verified"))
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
    )
    brain = RecordingBrain()
    writer = Writer(brain, state, cfg)
    writer.skills = ""
    await writer._write_personal_email(await state.get_prospect(pid))
    assert "replied to 2 of the 20 newest reviews" in brain.prompts[0]
    assert "Their newest Google review: 5 months ago" in brain.prompts[0]
