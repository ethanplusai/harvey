"""Extra model coverage: validation edge cases and malformed-input robustness."""

import pytest
from pydantic import ValidationError

from sellbird.models.prospect import Prospect
from sellbird.models.company import Company
from sellbird.models.campaign import Campaign, EmailStep
from sellbird.models.conversation import Conversation, Message, STAGES


# ── Prospect ──

def test_prospect_full_name_strips_when_partial():
    assert Prospect(first_name="Jane", title="VP").full_name() == "Jane"
    assert Prospect(last_name="Doe", title="VP").full_name() == "Doe"
    assert Prospect().full_name() == ""


def test_prospect_invalid_when_all_empty():
    assert not Prospect().is_valid()


def test_prospect_defaults():
    p = Prospect()
    assert p.status == "new"
    assert p.score == 0
    assert p.email_verified is False
    assert p.phone_verified is False
    assert p.created_at is not None


def test_prospect_rejects_non_coercible_score():
    with pytest.raises(ValidationError):
        Prospect(first_name="J", last_name="D", title="VP", score="high")


def test_prospect_coerces_numeric_string_score():
    p = Prospect(first_name="J", last_name="D", title="VP", score="7")
    assert p.score == 7


# ── Company ──

def test_company_all_fields_default_empty():
    c = Company()
    for field in ("id", "name", "domain", "website", "description",
                  "industry", "company_size", "location", "source",
                  "source_url", "notes"):
        assert getattr(c, field) == ""


# ── Campaign / EmailStep ──

def test_email_step_requires_subject_and_body():
    with pytest.raises(ValidationError):
        EmailStep(step=1)


def test_email_step_delay_defaults_to_zero():
    s = EmailStep(step=1, subject="s", body="b")
    assert s.delay_days == 0


def test_campaign_empty_sequence_round_trip():
    c = Campaign(id="c1")
    assert c.sequence_json() == "[]"
    assert Campaign.sequence_from_json("[]") == []


def test_campaign_sequence_from_malformed_json_returns_empty():
    # Corrupt/NULL stored JSON is tolerated and treated as an empty sequence.
    assert Campaign.sequence_from_json("not valid json {{{") == []
    assert Campaign.sequence_from_json(None) == []
    assert Campaign.sequence_from_json("") == []


def test_campaign_sequence_from_json_bad_step_shape_raises():
    with pytest.raises(ValidationError):
        Campaign.sequence_from_json('[{"unexpected": "shape"}]')


def test_campaign_requires_id_field():
    with pytest.raises(ValidationError):
        Campaign()


def test_campaign_sequence_json_unicode_round_trip():
    steps = [EmailStep(step=1, subject="Héllo — “quotes”", body="日本語 body")]
    restored = Campaign.sequence_from_json(
        Campaign(id="c1", sequence=steps).sequence_json()
    )
    assert restored[0].subject == "Héllo — “quotes”"
    assert restored[0].body == "日本語 body"


# ── Conversation / Message ──

def test_message_requires_sender_and_content():
    with pytest.raises(ValidationError):
        Message(sender="sellbird")
    with pytest.raises(ValidationError):
        Message(content="hi")


def test_conversation_requires_prospect_id():
    with pytest.raises(ValidationError):
        Conversation(id="x")


def test_conversation_defaults():
    convo = Conversation(id="x", prospect_id="p1")
    assert convo.stage == "initial_outreach"
    assert convo.status == "open"
    assert convo.channel == "email"
    assert convo.thread == []
    assert convo.intent == ""


def test_conversation_default_stage_is_first_pipeline_stage():
    assert Conversation(id="x", prospect_id="p1").stage == STAGES[0]
    assert "closed_won" in STAGES and "closed_lost" in STAGES


def test_conversation_empty_thread_round_trip():
    convo = Conversation(id="x", prospect_id="p1")
    assert convo.thread_json() == "[]"
    assert Conversation.thread_from_json("[]") == []


def test_thread_json_preserves_timestamps():
    convo = Conversation(
        id="x", prospect_id="p1",
        thread=[Message(sender="sellbird", content="Hi")],
    )
    original_ts = convo.thread[0].timestamp
    restored = Conversation.thread_from_json(convo.thread_json())
    assert restored[0].timestamp == original_ts


def test_thread_from_malformed_json_returns_empty():
    # Corrupt/NULL stored JSON is tolerated and treated as an empty thread.
    assert Conversation.thread_from_json("{broken") == []
    assert Conversation.thread_from_json(None) == []
    assert Conversation.thread_from_json("") == []
