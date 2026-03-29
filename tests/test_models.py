"""Tests for data models."""

from harvey.models.prospect import Prospect
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.company import Company
from harvey.models.conversation import Conversation, Message


def test_prospect_is_valid():
    """Prospect requires first_name, last_name, and title."""
    p = Prospect(first_name="Jane", last_name="Doe", title="VP of Sales")
    assert p.is_valid()


def test_prospect_invalid_missing_title():
    p = Prospect(first_name="Jane", last_name="Doe")
    assert not p.is_valid()


def test_prospect_invalid_missing_name():
    p = Prospect(title="VP of Sales")
    assert not p.is_valid()


def test_prospect_full_name():
    p = Prospect(first_name="Jane", last_name="Doe", title="VP")
    assert p.full_name() == "Jane Doe"


def test_campaign_sequence_json_round_trip():
    steps = [
        EmailStep(step=1, subject="Hi", body="Hello there", delay_days=0),
        EmailStep(step=2, subject="Follow up", body="Just checking in", delay_days=3),
    ]
    c = Campaign(id="", name="Test", channel="email", sequence=steps, prospect_ids=["p1", "p2"])
    json_str = c.sequence_json()
    restored = Campaign.sequence_from_json(json_str)
    assert len(restored) == 2
    assert restored[0].subject == "Hi"
    assert restored[1].delay_days == 3


def test_company_defaults():
    c = Company(name="Acme", domain="acme.com")
    assert c.website == ""
    assert c.industry == ""


def test_conversation_thread_json_round_trip():
    msgs = [
        Message(sender="harvey", content="Hi there"),
        Message(sender="prospect", content="Interested!"),
    ]
    convo = Conversation(
        id="", prospect_id="p1", campaign_id="c1", channel="email",
        thread=msgs, intent="interested", status="open",
    )
    json_str = convo.thread_json()
    restored = Conversation.thread_from_json(json_str)
    assert len(restored) == 2
    assert restored[0].sender == "harvey"
    assert restored[1].content == "Interested!"
