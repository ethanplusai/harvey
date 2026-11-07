"""The offer in ``product.offer`` shapes what a reply proposes.

The config asked for a goal and a booking method all along, but only the
booking URL was read (by the send gate). Every reply was told to suggest
2-3 times, so a Calendly link never appeared and a trial never got offered.
These tests pin the instruction the model receives; no email is sent.

The clock is pinned, so the dates are stable in every time zone the tests
run in.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird import brain as brain_mod
from sellbird.booking import booking_instruction
from sellbird.config import SendWindowConfig
from sellbird.models.conversation import Conversation, Message
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager
from tests.test_outbox_native import Cfg, Env
from tests.test_prompt_injection import RecordingBrain

# A Monday afternoon, so "tomorrow" is unambiguous.
NOW = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)


def _cfg(**offer) -> SimpleNamespace:
    fields = dict(
        goal="book_call", booking_method="calendar_link", booking_url="",
        entry="", meeting_duration="15 minutes", meeting_owner="",
    )
    fields.update(offer)
    return SimpleNamespace(
        product=SimpleNamespace(offer=SimpleNamespace(**fields)),
        channels=SimpleNamespace(email=SimpleNamespace(
            send_window=SendWindowConfig(start="09:00", end="17:00"),
        )),
        usage=SimpleNamespace(quiet_hours=SimpleNamespace(timezone="America/New_York")),
    )


# ── calendar link ──


def test_a_configured_calendar_link_is_offered_verbatim():
    text = booking_instruction(
        _cfg(booking_url="https://cal.com/sellbird/15", meeting_owner="Sam"),
        NOW,
    )
    assert "https://cal.com/sellbird/15" in text
    assert "Sam" in text
    assert "15 minutes" in text
    assert "Propose these times" not in text
    assert "Do not invent times" in text


def test_a_link_that_cannot_be_sent_falls_back_to_real_times():
    text = booking_instruction(_cfg(booking_url="javascript:alert(1)"), NOW)
    assert "javascript" not in text
    assert "Propose these times:" in text


def test_the_default_offer_suggests_times_when_no_link_is_set():
    """A fresh sellbird.yaml defaults to calendar_link with an empty URL.
    That must not leave the reply with nothing to offer."""
    text = booking_instruction(_cfg(), NOW)
    assert "Propose these times:" in text
    assert "Tuesday, November 3" in text


# ── suggested times ──


def test_suggested_times_start_after_today_and_skip_the_weekend():
    text = booking_instruction(_cfg(booking_method="suggest_times"), NOW)
    assert "Tuesday, November 3" in text
    assert "Wednesday, November 4" in text
    assert "Thursday, November 5" in text
    assert "Monday, November 2" not in text
    assert "Saturday" not in text and "Sunday" not in text


def test_a_friday_only_offers_the_next_week():
    friday = datetime(2026, 11, 6, 14, 0, tzinfo=timezone.utc)
    text = booking_instruction(_cfg(booking_method="suggest_times"), friday)
    assert "Monday, November 9" in text
    assert "Saturday" not in text and "Sunday" not in text


def test_the_window_hours_bound_the_suggested_times():
    config = _cfg(booking_method="suggest_times")
    config.channels.email.send_window = SendWindowConfig(start="07:30", end="09:30")
    text = booking_instruction(config, NOW)
    assert "8:30 AM" in text
    assert "10:00 AM" not in text


def test_the_recipients_zone_moves_the_date():
    """23:30 UTC is already Tuesday in Tokyo, so tomorrow is Wednesday."""
    late = datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)
    text = booking_instruction(_cfg(booking_method="suggest_times"), late, "Asia/Tokyo")
    assert "Wednesday, November 4" in text
    assert "Asia/Tokyo" in text
    assert "Tuesday, November 3" not in text


def test_without_a_zone_the_operator_clock_decides_but_no_zone_is_named():
    # 02:00 UTC Tuesday is still Monday evening in New York, so tomorrow is
    # Tuesday there.
    late = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)
    text = booking_instruction(_cfg(booking_method="suggest_times"), late, "")
    assert "Tuesday, November 3" in text
    assert "America/New_York" not in text
    assert "time zone" in text


def test_an_unknown_zone_is_ignored_rather_than_printed():
    text = booking_instruction(_cfg(booking_method="suggest_times"), NOW, "Mars/Olympus")
    assert "Mars/Olympus" not in text
    assert "Propose these times:" in text


# ── other goals and methods ──


def test_asking_preference_does_not_invent_a_time():
    text = booking_instruction(_cfg(booking_method="ask_preference"), NOW)
    assert "what time works for them" in text
    assert "AM" not in text and "PM" not in text
    assert "15 minutes" in text


def test_start_trial_offers_the_entry_offer():
    text = booking_instruction(_cfg(goal="start_trial", entry="a free audit"), NOW)
    assert "a free audit" in text
    assert "one action" in text


def test_start_trial_without_an_entry_still_offers_a_next_step():
    text = booking_instruction(_cfg(goal="start_trial", booking_method="suggest_times"), NOW)
    assert "Propose these times:" in text


def test_get_reply_does_not_push_a_meeting():
    text = booking_instruction(
        _cfg(goal="get_reply", booking_url="https://cal.com/sellbird/15"), NOW,
    )
    assert "Do not propose a call" in text
    assert "https://cal.com/sellbird/15" not in text


# ── the reply prompt ──


class PromptBrain(RecordingBrain):
    """Records the prompt, but loads the real prompt file so the handler's
    ``{{booking}}`` and ``{{stage}}`` are checked, not a stub."""

    def load_prompt(self, name, **kwargs):
        template = (brain_mod.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
        for key, value in kwargs.items():
            template = template.replace(f"{{{{{key}}}}}", str(value))
        return template


class OfferCfg(Cfg):
    class product(Cfg.product):
        class offer:
            goal = "book_call"
            booking_method = "calendar_link"
            booking_url = "https://cal.example/sellbird/15"
            entry = ""
            meeting_duration = "20 minutes"
            meeting_owner = "Sam"


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "booking.db"))
    await s.init_db()
    return s


async def _handler(state, config=None):
    from sellbird.agents.handler import Handler

    brain = PromptBrain(text_reply="Happy to. Tuesday works?")
    handler = Handler(brain=brain, state=state, config=config or OfferCfg(), env=Env())
    return handler, brain


async def _prospect(state):
    pid = await state.add_prospect(Prospect(
        first_name="Jo", last_name="Q", title="CTO", company="Q Co",
        email="jo@q.example", email_status="verified",
    ))
    return await state.get_prospect(pid)


def _convo(prospect, stage="engaged"):
    return Conversation(
        id="c", prospect_id=prospect.id, stage=stage,
        thread=[Message(sender="prospect", content="Let's talk.")],
    )


@pytest.mark.asyncio
async def test_an_interested_reply_gets_the_configured_booking_link(state):
    handler, brain = await _handler(state)
    prospect = await _prospect(state)
    await handler._generate_response(
        "interested", "Let's talk.", prospect, _convo(prospect),
    )
    prompt = brain.prompts[0]
    assert "https://cal.example/sellbird/15" in prompt
    assert "20 minutes" in prompt and "Sam" in prompt
    assert "{{" not in prompt


@pytest.mark.asyncio
async def test_a_question_does_not_get_a_pitch(state):
    handler, brain = await _handler(state)
    prospect = await _prospect(state)
    await handler._generate_response(
        "question", "What does it cost?", prospect, _convo(prospect, stage="engaged"),
    )
    prompt = brain.prompts[0]
    assert "Do not offer a meeting, a call, a trial, or a booking link" in prompt
    assert "https://cal.example/sellbird/15" not in prompt


@pytest.mark.asyncio
async def test_a_closing_thread_gets_the_next_step_whatever_the_intent(state):
    handler, brain = await _handler(state)
    prospect = await _prospect(state)
    await handler._generate_response(
        "question", "Send the details.", prospect, _convo(prospect, stage="closing"),
    )
    assert "https://cal.example/sellbird/15" in brain.prompts[0]
