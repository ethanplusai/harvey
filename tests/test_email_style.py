"""Sellbird's hard writing rules: what they catch, what they leave alone,
and that the Writer, the Sender and the Outbox editor all enforce them."""

from types import SimpleNamespace

import pytest

from sellbird.email_style import (
    BREAKUP_MAX_WORDS, FIRST_EMAIL_MAX_WORDS, style_problems, tidy, with_sign_off,
)

CLEAN = "Sam, 40 of your reviews mention slow callbacks. Who answers the phone after 5?"


def test_a_short_plain_email_passes():
    assert style_problems("missed calls", CLEAN) == []


@pytest.mark.parametrize("body", [
    "Sam, saw the new site — nice work. Who handles leads?",
    "Sam, saw the new site – nice work. Who handles leads?",
    "Sam, saw the new site - nice work. Who handles leads?",
    "Sam, saw the new site -- nice work. Who handles leads?",
    "Sam, saw the new site--nice work. Who handles leads?",
])
def test_every_kind_of_dash_between_words_is_refused(body):
    assert any("dash" in r for r in style_problems("new site", body))


@pytest.mark.parametrize("body", [
    "Sam, your follow-up emails go out 2-5 days apart. Who sets that?",
    "Sam, the 24-hour line at roofing-pros.com rings out. Who covers nights?",
])
def test_hyphens_inside_words_are_fine(body):
    assert style_problems("follow-ups", body) == []


@pytest.mark.parametrize("body, tell", [
    ("Hope this finds you well. Who runs ops?", "hope this finds you"),
    ("Sam, we can supercharge your pipeline. Who runs ops?", "supercharge"),
    ("Sam, it's not about more leads. Who runs ops?", "it's not about"),
    ("Sam, it’s not about more leads. Who runs ops?", "it's not about"),
    ("Sam, not just faster but cheaper. Who runs ops?", "not just X but Y"),
    ("Sam, worth a quick chat about ops?", "worth a quick"),
])
def test_ai_and_sales_phrases_are_refused(body, tell):
    reasons = style_problems("ops", body)
    assert any(tell in r for r in reasons), reasons


def test_the_prospects_own_name_is_never_a_tell():
    body = "Sam, Elevate Fitness opened a third gym. Who books the trainers?"
    assert style_problems("third gym", body, names=["Elevate Fitness"]) == []
    assert style_problems("third gym", body) != []


def test_exclamation_marks_and_formatting_are_refused():
    assert style_problems("hi", "Sam, great news! Who runs ops?")
    assert style_problems("hi", "Sam, two things:\n- calls\n- texts\nWho runs ops?")
    assert style_problems("hi", "Sam, **calls** are down. Who runs ops?")


def test_cold_email_shape_word_caps_by_step():
    long_first = "Sam, " + "word " * FIRST_EMAIL_MAX_WORDS + "ok?"
    assert any("words" in r for r in style_problems("x y", long_first, step=1))
    breakup = "Sam, " + "word " * BREAKUP_MAX_WORDS + "ok?"
    assert any("words" in r for r in style_problems("x y", breakup, step=3))
    assert not any("words" in r for r in style_problems("x y", breakup, step=1))


def test_cold_email_shape_one_question_and_not_about_you():
    assert any("question" in r for r in style_problems("x", "Sam, who runs ops? Who books?"))
    assert any('"I"' in r for r in style_problems("x", "Hi Sam,\n\nI noticed your site. Who runs ops?"))
    assert style_problems("x", "Hi Sam,\n\nYour site lists 3 crews. Who runs ops?") == []


def test_operator_rules_replace_the_shape_but_not_the_always_rules():
    long_two_questions = "Sam, " + "word " * 80 + "ok? and this?"
    assert style_problems("x", long_two_questions, shape=False) == []
    assert style_problems("x", "Sam, great news! Who?", shape=False)


def test_tidy_straightens_what_a_model_types_fancy():
    assert tidy("It’s “done”… ok  ") == "It's \"done\"... ok"


@pytest.mark.parametrize("body", [
    "Who runs ops?\n\nBest,\nAlex",
    "Who runs ops?\n\nThanks,\nAlex Kim",
    "Who runs ops?\n\nCheers, Alex",
    "Who runs ops?\n\n- Alex",
    "Who runs ops?",
])
def test_every_email_ends_with_the_same_plain_sign_off(body):
    assert with_sign_off(body, "Alex Kim") == "Who runs ops?\n\nAlex"


def test_no_persona_name_leaves_the_body_alone():
    assert with_sign_off("Who runs ops?\n\nBest,\nJo", "") == "Who runs ops?\n\nBest,\nJo"


# ── The Writer enforces them ──────────────────────────────────────────────

class FixingBrain:
    """Returns the queued answers in order, recording each prompt."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


def _writer(brain, rules=""):
    from sellbird.agents.writer import Writer
    w = Writer.__new__(Writer)
    w.brain = brain
    w.email_rules = rules
    w.config = SimpleNamespace(persona=SimpleNamespace(name="Alex Kim"))
    return w


@pytest.mark.asyncio
async def test_writer_rewrites_a_draft_once_and_keeps_the_fix():
    brain = FixingBrain([{"subject": "missed calls", "body": CLEAN}])
    draft = {"subject": "missed calls", "body": "Sam, slow callbacks — who answers after 5?"}
    out = await _writer(brain)._keep_to_rules(draft, step=1)
    assert out == {"subject": "missed calls", "body": CLEAN + "\n\nAlex"}
    assert "uses a dash between words" in brain.prompts[0]


@pytest.mark.asyncio
async def test_writer_drops_a_draft_the_rewrite_cannot_fix():
    still_bad = {"subject": "x", "body": "Sam, great! Who?"}
    out = await _writer(FixingBrain([still_bad]))._keep_to_rules(
        {"subject": "x", "body": "Sam, wow! Who?"}, step=1)
    assert out is None


@pytest.mark.asyncio
async def test_writer_needs_no_call_for_a_clean_draft():
    brain = FixingBrain([])
    out = await _writer(brain)._keep_to_rules(
        {"subject": "missed calls", "body": CLEAN + "\n\nBest,\nAlex"}, step=1)
    assert out["body"] == CLEAN + "\n\nAlex" and brain.prompts == []


@pytest.mark.asyncio
async def test_a_rewrite_may_not_invent_a_merge_variable():
    brain = FixingBrain([{"subject": "x", "body": "{{first_name}}, who answers at {{title}}?"}])
    out = await _writer(brain)._keep_to_rules(
        {"subject": "x", "body": "{{first_name}}, wow! Who answers?"}, step=1)
    assert out is None


@pytest.mark.asyncio
async def test_with_operator_rules_there_is_no_added_sign_off():
    out = await _writer(FixingBrain([]), rules="Sign off as The Team.")._keep_to_rules(
        {"subject": "x", "body": CLEAN + "\n\nThe Team"}, step=1)
    assert out["body"] == CLEAN + "\n\nThe Team"


# ── The Sender holds them ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sender_holds_an_email_with_a_dash_for_review(tmp_path):
    from sellbird.agents.sender import Sender
    from sellbird.state import HELD_FOR_REVIEW
    sender = Sender.__new__(Sender)
    sender.config = SimpleNamespace()

    class State:
        async def is_suppressed(self, _):
            return ""

    sender.state = State()
    import sellbird.agents.sender as mod

    async def no_sync(*a, **k):
        return None

    mod_sync, mod.sync_sending_mode = mod.sync_sending_mode, no_sync
    try:
        prospect = SimpleNamespace(company="Acme", first_name="Sam", last_name="Lee")
        item = {"kind": "reply", "subject": "Re: hi", "body": "Sure — Thursday works.",
                "to_email": "sam@acme.com"}
        status, error = await sender._blocked(item, prospect, allow_risky=False)
    finally:
        mod.sync_sending_mode = mod_sync
    assert status == "pending_review"
    assert error.startswith(HELD_FOR_REVIEW + "the email uses a dash")
