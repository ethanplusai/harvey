"""The eval loop: rule checks on drafts, a record of reviewers' edits, and a
scoreboard of outcomes per segment and step."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import pytest

from sellbird.draft_review import parse_call_hours, review_draft
from sellbird.evals import edit_diff, scoreboard, segment_for, since_days
from sellbird.models import Company
from sellbird.models.conversation import Conversation
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

NOW = datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc)
HOURS = dict(call_hours="07:00-10:00", call_hours_timezone="America/Chicago", now=NOW)
SIGNED = "\n\nDana\nFounder, Example Co"


def _flags(body, **kw):
    return review_draft("quick question", body, sender_name="Dana Reyes", **kw)


# ── Draft rules ──

def test_clean_draft_has_no_flags():
    body = "Hi Sam,\n\nIs live shopping on your roadmap this quarter?" + SIGNED
    assert _flags(body, country="US", facts="", **HOURS) == []


def test_missing_sign_off_is_flagged():
    flags = _flags("Hi Sam,\n\nIs live shopping on your roadmap this quarter?")
    assert any(f.startswith("No sign-off") for f in flags)


def test_closing_word_above_the_name_counts_as_the_sign_off():
    body = "Hi Sam,\n\nWorth a look?\n\nBest,\nDana"
    assert _flags(body) == []


def test_sender_written_about_in_the_third_person():
    body = "Hi Sam,\n\nA 20-minute call with Dana could make sense." + SIGNED
    assert any("Mentions Dana" in f for f in _flags(body))


def test_long_line_naming_the_sender_is_not_a_sign_off():
    body = "Hi Sam,\n\nIt's worth a short call with Dana before the quarter ends, I think."
    flags = _flags(body)
    assert any(f.startswith("No sign-off") for f in flags)
    assert any("Mentions Dana" in f for f in flags)


def test_times_inside_call_hours_pass_in_any_zone():
    body = ("Would Tue at 8:00am CT, 10:00am ET, 1:00pm GMT or 7:00am PT work?" + SIGNED)
    assert _flags(body, **HOURS) == []


def test_time_outside_call_hours_is_flagged_with_the_converted_time():
    body = "Would Tue at 10:00am PT work?" + SIGNED
    flags = _flags(body, **HOURS)
    assert flags == ["'10:00am PT' is 12:00pm in America/Chicago, outside your call hours (07:00-10:00)."]


def test_a_range_is_checked_at_both_ends():
    assert _flags("Anytime 8:00–11:00am ET works." + SIGNED, **HOURS) == []
    flags = _flags("Anytime 9:00–11:00am CT works." + SIGNED, **HOURS)
    assert len(flags) == 1 and "outside your call hours" in flags[0]


def test_time_without_a_zone_is_flagged():
    flags = _flags("Does 4pm on Thursday work?" + SIGNED, **HOURS)
    assert flags == ["'4pm' has no time zone; add one so the reader knows whose clock it is."]


def test_no_call_hours_configured_only_checks_for_a_zone():
    assert _flags("Does 4pm ET on Thursday work?" + SIGNED) == []


def test_day_first_dates_are_flagged_for_us_readers_only():
    body = "Free on Thursday, 12 November?" + SIGNED
    assert any("day-first" in f for f in _flags(body, country="US"))
    assert _flags(body, country="GB") == []


def test_numeric_dates_are_ambiguous_everywhere():
    assert any("ambiguous" in f for f in _flags("How about 10/11?" + SIGNED, country="GB"))


def test_footer_is_not_counted_or_mistaken_for_the_end():
    footer = "1 Example St, Springfield, ST 00000, USA\nNot interested? Reply \"no thanks\"."
    body = "Hi Sam,\n\nWorth a look?" + SIGNED + "\n\n" + footer
    assert _flags(body, footer=footer) == []


def test_non_native_phrasing():
    flags = _flags("Kindly revert back with your thoughts." + SIGNED)
    assert "'kindly' reads as non-native to US and UK readers." in flags
    assert "'revert back' reads as non-native to US and UK readers." in flags


def test_figures_must_come_from_the_product_facts():
    facts = "We track 870k+ listings and 12,000 shops. Starter is $49/mo."
    body = "We track 870k+ listings, 12,000 shops and 1.2M creators, from $49." + SIGNED
    flags = _flags(body, facts=facts)
    assert flags == ["'1.2M' isn't in your product facts: check the figure."]


def test_parse_call_hours():
    assert parse_call_hours("07:00-10:00") == (420, 600)
    assert parse_call_hours("10:00-07:00") is None
    assert parse_call_hours("") is None


# ── Reviewer edits ──

async def _state(tmp_path):
    state = StateManager(str(tmp_path / "sellbird.db"))
    await state.init_db()
    return state


async def _draft(state):
    return await state.add_outbox_item(
        prospect_id="p1", to_email="sam@example.com", subject="hi",
        body="Hi Sam,\n\nWorth a look?", send_at="2026-03-17T15:00:00")


def test_editing_a_draft_records_what_changed_and_why(tmp_path):
    async def run():
        state = await _state(tmp_path)
        item = await _draft(state)
        assert await state.edit_outbox_draft(item, "hi", "Hi Sam,\n\nWorth a look?\n\nDana",
                                             reason="added sign-off")
        revs = await state.get_outbox_revisions()
        assert len(revs) == 1
        r = revs[0]
        assert r["outbox_id"] == item and r["reason"] == "added sign-off"
        assert r["old_body"] == "Hi Sam,\n\nWorth a look?"
        assert r["new_body"].endswith("Dana")
        assert edit_diff(r) == "+\n+Dana"
    asyncio.run(run())


def test_a_direct_sql_edit_is_recorded_too(tmp_path):
    async def run():
        state = await _state(tmp_path)
        item = await _draft(state)
        async with aiosqlite.connect(state.db_path) as db:
            await db.execute("UPDATE outbox SET body = 'Changed' WHERE id = ?", (item,))
            await db.commit()
        assert [r["new_body"] for r in await state.get_outbox_revisions()] == ["Changed"]
    asyncio.run(run())


def test_status_changes_and_sent_mail_are_not_edits(tmp_path):
    async def run():
        state = await _state(tmp_path)
        item = await _draft(state)
        await state.approve_outbox(item)
        await state.update_outbox_item(item, status="sent", body="Hi Sam,\n\nWorth a look?\n\nfooter")
        assert await state.get_outbox_revisions() == []
    asyncio.run(run())


# ── Scoreboard ──

ICP = SimpleNamespace(icp=SimpleNamespace(
    industries=["widget analytics", "gadget marketplace"],
    keyword_variants={"widget analytics": ["widget tracker"]}))


def test_segment_rolls_search_phrases_up_to_their_industry():
    assert segment_for("widget analytics", ICP) == "widget analytics"
    assert segment_for("widget tracker", ICP) == "widget analytics"
    assert segment_for('"data engineer" careers widget analytics -site:example.com', ICP) == "widget analytics"
    assert segment_for("something else", ICP) == "other"
    assert segment_for("", ICP) == "other"


async def _company(state, name, found_by, email, email_status="verified"):
    cid = await state.add_company(Company(name=name, domain=email.split("@")[1], industry=found_by))
    pid = await state.add_prospect(Prospect(email=email, first_name="Sam", company=name,
                                            company_id=cid, email_status=email_status))
    return cid, pid


async def _sent(state, pid, email, step, at):
    item = await state.add_outbox_item(prospect_id=pid, to_email=email, subject="s", body="b",
                                       send_at=at, step=step, campaign_id=f"c-{pid}")
    await state.update_outbox_item(item, status="sent", sent_at=at)


def test_scoreboard_counts_companies_replies_and_meetings(tmp_path):
    async def run():
        state = await _state(tmp_path)
        t0 = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)
        at = lambda h: (t0 + timedelta(hours=h)).isoformat()
        _, a = await _company(state, "Alpha", "widget tracker", "a@alpha.example")
        _, b = await _company(state, "Beta", "widget analytics", "b@beta.example")
        _, c = await _company(state, "Gamma", "gadget marketplace", "c@gamma.example",
                              email_status="invalid")
        _, d = await _company(state, "Delta", "gadget marketplace", "d@delta.example")
        for pid, email in ((a, "a@alpha.example"), (b, "b@beta.example"),
                           (c, "c@gamma.example"), (d, "d@delta.example")):
            await _sent(state, pid, email, 1, at(0))
        await _sent(state, a, "a@alpha.example", 2, at(10))
        await state.add_conversation(Conversation(
            id="", prospect_id=a, intent="interested", stage="closing",
            created_at=t0 + timedelta(hours=20)))
        await state.add_conversation(Conversation(
            id="", prospect_id=d, intent="not_interested", stage="closed_lost",
            created_at=t0 + timedelta(hours=5)))
        await state.add_conversation(Conversation(
            id="", prospect_id=b, intent="ooo", created_at=t0 + timedelta(hours=5)))

        report = await scoreboard(state.db_path, ICP, since_days(30))
        assert report["total"] == {"companies": 4, "bounced": 1, "replied": 2, "positive": 1,
                                   "meetings": 1, "reply_rate": "67%"}
        assert report["segments"]["widget analytics"]["positive"] == 1
        assert report["segments"]["gadget marketplace"]["bounced"] == 1
        assert report["steps"] == {1: {"sent": 4, "replies_after": 1},
                                   2: {"sent": 1, "replies_after": 1}}
    asyncio.run(run())



# ── Replies are signed like cold email ──

from sellbird.agents.handler import Handler  # noqa: E402


def _handler(email_rules=""):
    return SimpleNamespace(config=SimpleNamespace(
        persona=SimpleNamespace(name="Dana Reyes"),
        writer=SimpleNamespace(email_rules=email_rules)))


def test_reply_gets_the_senders_first_name():
    assert Handler._signed(_handler(), "Thursday works.") == "Thursday works.\n\nDana"


def test_reply_keeps_a_single_sign_off():
    assert Handler._signed(_handler(), "Thursday works.\n\nBest,\nDana") == "Thursday works.\n\nDana"


def test_operator_email_rules_keep_their_own_sign_off():
    assert Handler._signed(_handler("skills/email_rules.md"), "Thursday works.") == "Thursday works."


def test_signed_reply_passes_the_sign_off_check():
    body = Handler._signed(_handler(), "Hi Sam,\n\nThursday works.")
    assert review_draft("Re: hi", body, sender_name="Dana Reyes", kind="reply") == []
