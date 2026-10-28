"""Extra coverage for the state layer: CRUD, dedup, analytics, robustness."""

import os
import json

import pytest
import pytest_asyncio

from harvey.state import StateManager
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.conversation import Conversation, Message


@pytest_asyncio.fixture
async def state(tmp_path):
    """StateManager backed by a temp SQLite DB."""
    sm = StateManager(str(tmp_path / "test.db"))
    await sm.init_db()
    yield sm


# ── Init / schema ──

@pytest.mark.asyncio
async def test_init_db_is_idempotent(tmp_path):
    sm = StateManager(str(tmp_path / "test.db"))
    await sm.init_db()
    await sm.init_db()  # second call must not raise
    assert os.path.exists(sm.db_path)


@pytest.mark.asyncio
async def test_init_db_creates_parent_dirs(tmp_path):
    sm = StateManager(str(tmp_path / "nested" / "dirs" / "test.db"))
    await sm.init_db()
    assert os.path.exists(sm.db_path)


# ── Companies ──

@pytest.mark.asyncio
async def test_get_company_by_id(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.com", location="NYC"))
    fetched = await state.get_company(cid)
    assert fetched is not None
    assert fetched.id == cid
    assert fetched.location == "NYC"


@pytest.mark.asyncio
async def test_get_company_missing_returns_none(state):
    assert await state.get_company("does-not-exist") is None
    assert await state.get_company_by_domain("nope.example") is None


@pytest.mark.asyncio
async def test_add_company_same_id_is_ignored(state):
    c1 = Company(id="fixed-id", name="First", domain="first.com")
    c2 = Company(id="fixed-id", name="Second", domain="second.com")
    await state.add_company(c1)
    await state.add_company(c2)  # INSERT OR IGNORE — must not raise
    fetched = await state.get_company("fixed-id")
    assert fetched.name == "First"  # original row preserved


@pytest.mark.asyncio
async def test_add_company_generates_id_when_missing(state):
    c = Company(name="NoId", domain="noid.com")
    cid = await state.add_company(c)
    assert cid
    assert c.id == cid


# ── Prospects ──

@pytest.mark.asyncio
async def test_get_prospect_by_id_roundtrips_bools(state):
    p = Prospect(
        first_name="Jane", last_name="Doe", email="jane@x.com",
        title="VP", email_verified=True, phone_verified=False, score=42,
    )
    pid = await state.add_prospect(p)
    fetched = await state.get_prospect(pid)
    assert fetched is not None
    assert fetched.email_verified is True
    assert fetched.phone_verified is False
    assert fetched.score == 42


@pytest.mark.asyncio
async def test_get_prospect_missing_returns_none(state):
    assert await state.get_prospect("nope") is None


@pytest.mark.asyncio
async def test_get_prospect_by_email_empty_string(state):
    assert await state.get_prospect_by_email("") is None


@pytest.mark.asyncio
async def test_prospect_exists_by_linkedin(state):
    p = Prospect(
        first_name="Jane", last_name="Doe", title="VP",
        linkedin_url="https://linkedin.com/in/janedoe",
    )
    await state.add_prospect(p)
    assert await state.prospect_exists(linkedin_url="https://linkedin.com/in/janedoe")
    assert not await state.prospect_exists(linkedin_url="https://linkedin.com/in/other")


@pytest.mark.asyncio
async def test_prospect_exists_name_company_case_insensitive(state):
    p = Prospect(first_name="Jane", last_name="Doe", title="VP", company="Acme Corp")
    await state.add_prospect(p)
    assert await state.prospect_exists(
        first_name="JANE", last_name="doe", company="ACME corp"
    )
    assert not await state.prospect_exists(
        first_name="Jane", last_name="Doe", company="Other Co"
    )


@pytest.mark.asyncio
async def test_prospect_exists_all_empty_args(state):
    await state.add_prospect(Prospect(first_name="A", last_name="B", title="C"))
    assert not await state.prospect_exists()


@pytest.mark.asyncio
async def test_add_prospect_same_id_is_ignored(state):
    p1 = Prospect(id="p-1", first_name="Jane", last_name="Doe", title="VP")
    p2 = Prospect(id="p-1", first_name="Evil", last_name="Twin", title="Dir")
    await state.add_prospect(p1)
    await state.add_prospect(p2)
    fetched = await state.get_prospect("p-1")
    assert fetched.first_name == "Jane"


@pytest.mark.asyncio
async def test_count_prospects_by_status(state):
    await state.add_prospect(Prospect(first_name="A", last_name="A", title="T", email="a@x.com"))
    await state.add_prospect(Prospect(first_name="B", last_name="B", title="T", email="b@x.com"))
    pid = await state.add_prospect(Prospect(first_name="C", last_name="C", title="T", email="c@x.com"))
    await state.update_prospect_status(pid, "contacted")
    counts = await state.count_prospects_by_status()
    assert counts.get("new") == 2
    assert counts.get("contacted") == 1


@pytest.mark.asyncio
async def test_update_prospect_status_nonexistent_id_noop(state):
    await state.update_prospect_status("ghost-id", "contacted")  # must not raise
    assert await state.get_prospects_by_status("contacted") == []


# ── Campaigns ──

@pytest.mark.asyncio
async def test_campaign_add_and_fetch_by_status(state):
    steps = [EmailStep(step=1, subject="Hi", body="Hello", delay_days=0)]
    c = Campaign(id="", name="Q3 Outreach", sequence=steps, prospect_ids=["p1"])
    cid = await state.add_campaign(c)
    assert cid

    drafts = await state.get_campaigns_by_status("draft")
    assert len(drafts) == 1
    assert drafts[0].name == "Q3 Outreach"
    assert drafts[0].sequence[0].subject == "Hi"
    assert drafts[0].prospect_ids == ["p1"]
    assert await state.get_campaigns_by_status("active") == []


@pytest.mark.asyncio
async def test_update_campaign_fields(state):
    cid = await state.add_campaign(Campaign(id="", name="Test"))
    await state.update_campaign(cid, status="active", instantly_campaign_id="inst-9")
    active = await state.get_campaigns_by_status("active")
    assert len(active) == 1
    assert active[0].instantly_campaign_id == "inst-9"


# ── Conversations ──

@pytest.mark.asyncio
async def test_conversation_add_and_fetch(state):
    convo = Conversation(
        id="", prospect_id="p1", campaign_id="c1",
        thread=[Message(sender="harvey", content="Hi")],
        intent="interested",
    )
    vid = await state.add_conversation(convo)
    assert vid

    open_convos = await state.get_conversations_by_status("open")
    assert len(open_convos) == 1
    assert open_convos[0].thread[0].content == "Hi"
    assert open_convos[0].stage == "initial_outreach"


@pytest.mark.asyncio
async def test_update_conversation_stage_and_status(state):
    vid = await state.add_conversation(Conversation(id="", prospect_id="p1"))
    await state.update_conversation(vid, stage="qualifying", status="replied")
    assert await state.get_conversations_by_status("open") == []
    replied = await state.get_conversations_by_status("replied")
    assert len(replied) == 1
    assert replied[0].stage == "qualifying"


@pytest.mark.asyncio
async def test_intent_and_stage_distribution(state):
    await state.add_conversation(Conversation(id="", prospect_id="p1", intent="interested"))
    await state.add_conversation(Conversation(id="", prospect_id="p2", intent="interested"))
    await state.add_conversation(
        Conversation(id="", prospect_id="p3", intent="objection", stage="negotiating")
    )
    await state.add_conversation(Conversation(id="", prospect_id="p4"))  # empty intent excluded

    intents = await state.get_intent_distribution()
    assert intents == {"interested": 2, "objection": 1}

    stages = await state.get_stage_distribution()
    assert stages.get("initial_outreach") == 3
    assert stages.get("negotiating") == 1


# ── Reply dedup ──

@pytest.mark.asyncio
async def test_is_reply_processed_empty_id(state):
    assert not await state.is_reply_processed("")


@pytest.mark.asyncio
async def test_reply_dedup_distinct_ids(state):
    await state.mark_reply_processed("r1")
    assert await state.is_reply_processed("r1")
    assert not await state.is_reply_processed("r2")


# ── Feedback ──

@pytest.mark.asyncio
async def test_feedback_crud(state):
    fid = await state.add_feedback("prospect", "p1", "Great fit")
    assert fid
    items = await state.get_feedback("prospect", "p1")
    assert len(items) == 1
    assert items[0]["comment"] == "Great fit"
    assert await state.get_feedback("prospect", "other") == []
    assert len(await state.get_all_feedback()) == 1


# ── Actions / usage / summary ──

@pytest.mark.asyncio
async def test_log_action_with_and_without_details(state):
    await state.log_action("prospect_added", "scout", {"count": 3})
    await state.log_action("idle", "analyst")  # details=None path must not raise


@pytest.mark.asyncio
async def test_usage_today_zero_when_empty(state):
    assert await state.get_usage_today() == 0


@pytest.mark.asyncio
async def test_state_summary_shape(state):
    await state.add_prospect(Prospect(first_name="A", last_name="B", title="T"))
    await state.add_campaign(Campaign(id="", name="C"))
    await state.add_conversation(Conversation(id="", prospect_id="p1"))
    await state.increment_usage()

    summary = await state.get_state_summary()
    assert summary["prospects"].get("new") == 1
    assert summary["draft_campaigns"] == 1
    assert summary["active_campaigns"] == 0
    assert summary["open_conversations"] == 1
    assert summary["usage_today"] == 1


@pytest.mark.asyncio
async def test_campaign_stats(state):
    c = Campaign(id="", name="Stats", status="active", prospect_ids=["p1", "p2", "p3", "p4"])
    cid = await state.add_campaign(c)
    await state.add_conversation(
        Conversation(id="", prospect_id="p1", campaign_id=cid, intent="interested")
    )
    await state.add_conversation(
        Conversation(id="", prospect_id="p2", campaign_id=cid, intent="not_interested")
    )

    stats = await state.get_campaign_stats()
    assert len(stats) == 1
    s = stats[0]
    assert s["leads_count"] == 4
    assert s["reply_count"] == 2
    assert s["interested_count"] == 1
    assert s["not_interested_count"] == 1
    assert s["reply_rate"] == 50.0


@pytest.mark.asyncio
async def test_campaign_stats_zero_prospects_no_division_error(state):
    await state.add_campaign(Campaign(id="", name="Empty", status="active"))
    stats = await state.get_campaign_stats()
    assert stats[0]["reply_rate"] == 0

# ── Email-status backfill ──

@pytest.mark.asyncio
async def test_state_backfill_query_selects_only_unconfirmed(state):
    await state.add_prospect(Prospect(
        first_name="Guess", last_name="Only", email="guess.only@acme.com",
        email_status="guess", company="Acme", status="new",
    ))
    await state.add_prospect(Prospect(
        first_name="Done", last_name="Already", email="done@acme.com",
        email_status="verified", email_verified=True, company="Acme", status="new",
    ))
    rows = await state.get_prospects_by_email_status(("", "guess"))
    assert [p.email for p in rows] == ["guess.only@acme.com"]
    assert await state.get_prospects_by_email_status(("", "guess"), domain="other.com") == []
    assert await state.get_prospects_by_email_status(
        ("", "guess"), domain="acme.com", limit=1)
