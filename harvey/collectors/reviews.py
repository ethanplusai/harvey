"""REPUTATION — what a business's customers say about it on Google.

Discovery already records two numbers from the listing: the star rating and
the review count. They say how a business is rated, not how it treats the
people rating it. This reads the newest reviews behind them: whether the
owner answers, when the last one came in, whether a bad one is sitting
unanswered, and what customers talk about most.

It costs money, so it is opt-in three times over: a reviews key in .env, a
confirmed reputation signal, and an explicit ``harvey reviews`` run under a
dollar cap. Nothing on the heartbeat calls it.

Only a business discovered from a Google Maps listing carries the place
reference this needs (``gmaps:<place_id>`` from Scavio Maps,
``dataforseo:...`` from the DataForSEO listings and Maps providers). One
found only by an organic search has none and is skipped.

No review text and no reviewer is ever stored. What a stranger wrote about a
business is not the sender's to quote back at its owner; the stars, the age
and whether the owner answered are the facts an email can stand on.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from harvey.collectors.discover import TIMEOUT, ProviderExhausted, ScavioProvider

logger = logging.getLogger("harvey.collectors.reviews")

REVIEW_CODES = ("REVIEW_REPLY_RATE", "LAST_REVIEW_DAYS", "NEW_REVIEWS_90D",
                "RECENT_BAD_REVIEW", "REVIEW_TOPICS")

RECENT_DAYS = 90         # NEW_REVIEWS_90D's window
BAD_REVIEW_DAYS = 180    # how old a 1-2 star review can be and still be "recent"
MIN_FOR_RATE = 5         # fewer reviews than this make a reply rate noise
STALE_FACT_DAYS = 180    # the writer ignores a reading older than this


@dataclass
class Review:
    rating: int | None
    when: datetime | None     # the date Google shows: the last edit, else the post
    answered: bool            # the owner replied


@dataclass
class ReviewSample:
    """The newest reviews of one listing, and what came with them."""

    reviews: list[Review]
    total: int | None = None                  # the listing's review count, if given
    more: bool = False                        # Google offered a next page
    topics: list[tuple[str, int]] = field(default_factory=list)
    evidence_url: str = ""

    @property
    def complete(self) -> bool:
        """Is this every review there is, or a full page of the newest?

        Google sometimes answers a reviews request with the listing's short
        overview (a handful of "most relevant" reviews, no next page) instead
        of the newest. Read as the newest, that makes a busy business look
        quiet, so a short page that isn't the whole listing counts for nothing.
        """
        n = len(self.reviews)
        return self.more or n >= ReviewsProvider.PAGE_SIZE or (
            self.total is not None and n >= self.total)


def place_ref(external_id: str) -> tuple[str, str] | None:
    """("place_id", "ChIJ...") or ("data_cid", "123..."): the Google listing a
    company was discovered from, or None when it wasn't one."""
    source, _, rest = (external_id or "").partition(":")
    if not rest:
        return None
    if source == "gmaps":
        return "place_id", rest
    if source == "dataforseo":
        kind, _, value = rest.partition(":")
        if kind == "cid" and value.isdigit():
            return "data_cid", value
        if not value:            # "dataforseo:<place_id>"
            return "place_id", rest
    return None


class ReviewsProvider:
    """One way of reading a Google listing's newest reviews.

    Subclasses implement ``read``. Spend is reported through ``last_cost``
    and bounded by ``remaining_budget``, the same contract discovery
    providers keep, so the run loop can stop between businesses.
    """

    key: str = ""
    label: str = ""
    cost_note: str = ""
    PAGE_SIZE = 20

    def estimate(self, refs: list[tuple[str, str]]) -> float:
        """Projected spend, in dollars, before anything is called."""
        return 0.0

    async def read(self, client: httpx.AsyncClient, env: dict,
                   ref: tuple[str, str]) -> ReviewSample:
        raise NotImplementedError


class ScavioReviews(ReviewsProvider, ScavioProvider):
    """Google Maps reviews through Scavio: the same key as Scavio Maps."""

    key = "scavio"
    label = "Scavio Google Maps reviews"
    cost_note = ("1 credit per business (20 newest reviews), 2 when it was found by "
                 "DataForSEO and needs a place lookup first; estimates use $0.01/credit")

    def estimate(self, refs):
        return sum(self.CREDIT_PRICE * (2 if kind == "data_cid" else 1)
                   for kind, _ in refs)

    async def read(self, client, env, ref):
        self.last_cost = 0.0
        kind, value = ref
        body = {kind: value}
        if kind == "data_cid":
            # The reviews endpoint takes a place id or a data id, not a cid.
            place = (await self._post(client, env, "/maps/place", body)).get("place_results")
            ids = {k: place.get(k) for k in ("data_id", "place_id")} if isinstance(place, dict) else {}
            ids = {k: v for k, v in ids.items() if isinstance(v, str) and v}
            if not ids:
                raise RuntimeError("Scavio: place not found")
            body = {"data_id": ids["data_id"]} if "data_id" in ids else {"place_id": ids["place_id"]}
        data = await self._post(client, env, "/maps/reviews",
                                dict(body, sort_by="newest", num=self.PAGE_SIZE))
        return self._sample(data)

    @staticmethod
    def _sample(data: dict) -> ReviewSample:
        items = data.get("reviews")
        if items is None:
            items = []
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            raise RuntimeError("Scavio: invalid reviews list")
        info = data.get("place_info") or data.get("place_results") or {}
        total = info.get("reviews") if isinstance(info, dict) else None
        if not _count(total):
            total = None
        pagination = data.get("pagination") or {}
        if not isinstance(pagination, dict):
            raise RuntimeError("Scavio: invalid pagination")
        topics = []
        for t in data.get("topics") or []:
            if isinstance(t, dict) and isinstance(t.get("keyword"), str) and _count(t.get("mentions")):
                topics.append((t["keyword"].strip(), int(t["mentions"])))
        meta = data.get("search_metadata") or {}
        return ReviewSample(
            reviews=[Review(rating=_stars(i.get("rating")),
                            when=_when(i.get("iso_date_of_last_edit")) or _when(i.get("iso_date")),
                            answered=bool(i.get("response")))
                     for i in items],
            total=total,
            more=bool(pagination.get("next_page_token")),
            topics=topics,
            evidence_url=(meta.get("google_maps_url") or "") if isinstance(meta, dict) else "",
        )


def _count(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _stars(value) -> int | None:
    if _count(value) and 1 <= value <= 5:
        return int(round(value))
    return None


def _when(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


REVIEW_PROVIDERS: dict[str, ReviewsProvider] = {p.key: p for p in (ScavioReviews(),)}
DEFAULT_REVIEWS_PROVIDER = "scavio"


def review_observations(sample: ReviewSample, company_id: str, confirmed: set[str],
                        run_id: str, now: datetime | None = None) -> list[dict]:
    """The confirmed reputation facts in one sample, as observation rows.

    Everything is measured over the newest reviews Google returned (up to
    20), and each row's text says so: "3 of the 20 newest" is a fact, "15%"
    on its own overstates what was read.
    """
    if not sample.complete:
        return []
    now = now or datetime.now(timezone.utc)
    reviews = sample.reviews
    n = len(reviews)
    rows: list[dict] = []

    def add(code, *, num=None, text=""):
        if code in confirmed:
            rows.append({
                "company_id": company_id, "signal_code": code,
                "collector": "reviews", "value_num": num, "value_text": text,
                "confidence": 1.0, "evidence_url": sample.evidence_url,
                "run_id": run_id,
            })

    if n >= MIN_FOR_RATE:
        answered = sum(r.answered for r in reviews)
        add("REVIEW_REPLY_RATE", num=round(100.0 * answered / n, 1),
            text=f"{answered} of the {n} newest")

    dated = [r for r in reviews if r.when is not None]
    if dated:
        newest = max(r.when for r in dated)
        add("LAST_REVIEW_DAYS", num=float(max(0, (now - newest).days)),
            text=newest.date().isoformat())

    if dated or sample.total == 0:
        recent = sum(1 for r in dated if (now - r.when).days <= RECENT_DAYS)
        # Every review read is recent and there are more: the true count is
        # higher than what one page can show.
        floor = recent == n and n > 0 and (sample.more or (sample.total or 0) > n)
        add("NEW_REVIEWS_90D", num=float(recent),
            text=f"at least {recent}" if floor else f"{recent} of the {n} newest")

    if dated:
        bad = [r for r in dated if r.rating is not None and r.rating <= 2
               and (now - r.when).days <= BAD_REVIEW_DAYS]
        if bad:
            worst = max(bad, key=lambda r: r.when)
            add("RECENT_BAD_REVIEW", num=1.0,
                text=(f"{worst.rating}-star, {worst.when.date().isoformat()}, "
                      f"{'answered' if worst.answered else 'no reply'}"))
        else:
            add("RECENT_BAD_REVIEW", num=0.0, text=f"none in the {n} newest")

    if sample.topics:
        top = sorted(sample.topics, key=lambda t: -t[1])[:5]
        add("REVIEW_TOPICS", text="; ".join(f"{k} ({m})" for k, m in top))
    return rows


@dataclass
class ReviewsReport:
    provider: str
    companies: int = 0          # businesses this run would read
    read: int = 0
    partial: int = 0            # answered, but not with the newest reviews
    failed: int = 0
    observations: int = 0
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    run_id: str = ""
    stopped: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "provider", "companies", "read", "partial", "failed", "observations",
            "estimated_cost", "actual_cost", "run_id", "stopped", "errors")}


async def run_reviews(
    state,
    provider_key: str = DEFAULT_REVIEWS_PROVIDER,
    limit: int = 50,
    max_spend: float = 1.0,
    stale_days: int = 90,
    dry_run: bool = False,
) -> ReviewsReport:
    """Read the newest Google reviews of businesses found on Google Maps.

    ``dry_run`` estimates and returns without calling anything. Each
    business is stamped as checked once its reviews were paid for, even
    when nothing could be recorded, so a run rotates through the list
    instead of buying the same answer twice.
    """
    from harvey.config import load_env
    from harvey.signals import seed_signal_catalog

    provider = REVIEW_PROVIDERS.get(provider_key)
    if provider is None:
        raise ValueError(f"unknown reviews provider: {provider_key!r}")
    report = ReviewsReport(provider=provider_key)

    pending = []
    for c in await state.companies_needing_reviews(limit=limit, stale_days=stale_days):
        ref = place_ref(c.get("external_id") or "")
        if ref:
            pending.append((c, ref))
    report.companies = len(pending)
    report.estimated_cost = provider.estimate([ref for _, ref in pending])
    if dry_run or not pending:
        return report

    env = load_env().model_dump()
    missing = provider.missing_keys(env)
    if missing:
        names = ", ".join(k.upper() for k in missing)
        report.errors.append(f"{provider.label} is not ready: {names} is empty or "
                             f"still the template placeholder in .env")
        report.stopped = "not configured"
        return report

    if report.estimated_cost > max_spend:
        report.errors.append(
            f"estimated ${report.estimated_cost:.2f} exceeds the ${max_spend:.2f} "
            f"cap — raise the cap or read fewer businesses")
        report.stopped = "over budget"
        return report

    await seed_signal_catalog(state)
    confirmed = await state.confirmed_signal_codes() & set(REVIEW_CODES)
    if not confirmed:
        report.errors.append(
            "no reputation signals confirmed — confirm one first (dashboard → "
            "Signals, or `harvey signals --confirm REVIEW_REPLY_RATE`), or this "
            "run would pay for reviews and record nothing")
        report.stopped = "no confirmed signals"
        return report

    report.run_id = await state.start_run(
        "reviews", provider=provider_key,
        params={"companies": len(pending), "estimate": report.estimated_cost})
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            for company, ref in pending:
                # The switch that stops paid discovery stops paid review
                # reads too, and it is read between businesses.
                if await state.get_setting("discovery_paused"):
                    report.stopped = "paused"
                    break
                provider.last_cost = 0.0
                provider.remaining_budget = max(0.0, max_spend - report.actual_cost)
                try:
                    sample = await provider.read(client, env, ref)
                except ProviderExhausted as e:
                    report.actual_cost += provider.last_cost
                    logger.warning("reviews: %s stopped: %s", provider_key, e)
                    report.stopped = str(e)
                    break
                except Exception as e:
                    # A cid lookup can be paid for and still end here.
                    report.actual_cost += provider.last_cost
                    report.failed += 1
                    report.errors.append(f"{company.get('name') or company['id']}: {e}")
                    if provider.last_cost:
                        await state.mark_company_reviews_checked(company["id"])
                    continue
                report.actual_cost += provider.last_cost
                await state.mark_company_reviews_checked(company["id"])
                if not sample.complete:
                    report.partial += 1
                    continue
                report.read += 1
                report.observations += await state.add_observations(
                    review_observations(sample, company["id"], confirmed, report.run_id),
                    run_id=report.run_id)
        await state.finish_run(report.run_id, status="completed", records=report.read,
                               cost_usd=report.actual_cost)
    except Exception as e:
        logger.exception("reviews run failed")
        report.errors.append(str(e))
        await state.finish_run(report.run_id, status="failed", records=report.read,
                               cost_usd=report.actual_cost, error=str(e))
    return report


def _ago(days: int) -> str:
    if days < 2:
        return "a day ago" if days == 1 else "today"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{round(days / 7)} weeks ago"
    if days < 548:
        return f"{round(days / 30.4)} months ago"
    return f"{round(days / 365.25)} years ago"


def _observed(row: dict) -> datetime | None:
    return _when(str(row.get("observed_at") or "").replace(" ", "T"))


async def review_facts(state, company_id: str, now: datetime | None = None) -> list[str]:
    """Fact lines for the writer's prompt from the company's latest
    reputation readings, aged to today. [] when there are none, or when the
    last reading is too old to state as current. A failed read is just
    "nothing known", never a reason not to write."""
    if not company_id or state is None:
        return []
    now = now or datetime.now(timezone.utc)
    try:
        rows = await state.get_observations(company_id=company_id, latest_only=True)
    except Exception:
        return []
    latest = {r["signal_code"]: r for r in rows
              if r.get("collector") == "reviews" and r.get("signal_code") in REVIEW_CODES}
    facts = []
    for code in REVIEW_CODES:
        row = latest.get(code)
        seen = _observed(row) if row else None
        if seen is None:
            continue
        age = max(0, (now - seen).days)
        if age > STALE_FACT_DAYS:
            continue
        num, text = row.get("value_num"), row.get("value_text") or ""
        if code == "REVIEW_REPLY_RATE" and text:
            facts.append(f"- Google reviews: the business replied to {text} reviews")
        elif code == "LAST_REVIEW_DAYS" and num is not None:
            facts.append(f"- Their newest Google review: {_ago(int(num) + age)}")
        elif code == "NEW_REVIEWS_90D" and num is not None and age <= RECENT_DAYS:
            count = text if text.startswith("at least") else str(int(num))
            facts.append(f"- New Google reviews in the 90 days before "
                         f"{seen.date().isoformat()}: {count}")
        elif code == "RECENT_BAD_REVIEW" and num == 1:
            stars, posted, reply = (text.split(", ") + ["", "", ""])[:3]
            when = _when(posted + "T00:00:00+00:00")
            if when and (now - when).days <= BAD_REVIEW_DAYS:
                facts.append(f"- A {stars} Google review from {_ago((now - when).days)} "
                             + ("has no reply from the business" if reply == "no reply"
                                else "that the business replied to"))
        elif code == "REVIEW_TOPICS" and text:
            facts.append(f"- What their Google reviews mention most: {text.replace('; ', ', ')}")
    return facts
