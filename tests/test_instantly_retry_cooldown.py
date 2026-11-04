"""Regression tests for the Instantly deployment retry cooldown.

When an Instantly API operation fails mid-deploy the campaign stays ``draft``
and the heartbeat kept picking ``send_campaign`` every cycle, starving the
writer and prospecting agents. Each failed deploy now stamps a persistent
retry timestamp (``instantly_deploy_retry_after:{campaign_id}``) and deferred
drafts are excluded from the deployable count until the cooldown expires.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from harvey.agents.sender import Sender
from harvey.main import decide_next_action
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.state import StateManager, _utcnow
from tests.test_outbox_native import Env


def instantly_config(require_approval=False):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            enabled=True, provider="instantly", require_approval=require_approval,
            max_daily_sends=50, send_to_risky=False)),
        persona=SimpleNamespace(name="Harvey", role="BDR", company="Acme", tone="direct"),
        product=SimpleNamespace(name="Widget", description="A widget", objection_responses={}),
    )


GOOD_STEP = EmailStep(step=1, subject="a note", body="Would this help?", delay_days=0)


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "cooldown.db"))
    await state.init_db()
    return state


async def add_campaign(state, instantly_campaign_id="", prospect_ids=None):
    campaign = Campaign(
        id="", name="Draft", instantly_campaign_id=instantly_campaign_id,
        sequence=[GOOD_STEP], prospect_ids=prospect_ids or [], status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return campaign


# ── 1. An Instantly API failure records a persistent retry timestamp ──


@pytest.mark.asyncio
async def test_create_campaign_failure_records_retry_timestamp(state):
    sender = Sender(None, state, instantly_config(False), Env())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = None  # transient failure
    campaign = await add_campaign(state)
    await sender.run()
    value = await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}")
    assert value != ""
    retry_after = datetime.fromisoformat(value)
    assert retry_after > _utcnow()


@pytest.mark.asyncio
async def test_set_campaign_emails_failure_records_retry_timestamp(state):
    sender = Sender(None, state, instantly_config(False), Env())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = {"id": "remote-1"}
    sender.instantly.set_campaign_emails.return_value = None  # transient failure
    campaign = await add_campaign(state, instantly_campaign_id="")
    await sender.run()
    value = await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}")
    assert value != ""
    assert (await state.get_campaigns_by_status("draft"))[0].instantly_campaign_id == "remote-1"


@pytest.mark.asyncio
async def test_add_leads_failure_records_retry_timestamp(state):
    sender = Sender(None, state, instantly_config(False), Env())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = {"id": "remote-1"}
    sender.instantly.set_campaign_emails.return_value = {}
    sender.instantly.add_leads.return_value = None  # transient failure
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="Owner", company="Acme",
        email="jane@acme.test", email_status="verified",
    ))
    campaign = await add_campaign(state, instantly_campaign_id="", prospect_ids=[pid])
    await sender.run()
    value = await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}")
    assert value != ""


@pytest.mark.asyncio
async def test_activate_campaign_failure_records_retry_timestamp(state):
    sender = Sender(None, state, instantly_config(False), Env())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = {"id": "remote-1"}
    sender.instantly.set_campaign_emails.return_value = {}
    sender.instantly.add_leads.return_value = {}
    sender.instantly.activate_campaign.return_value = None  # transient failure
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="Owner", company="Acme",
        email="jane@acme.test", email_status="verified",
    ))
    campaign = await add_campaign(state, instantly_campaign_id="", prospect_ids=[pid])
    await sender.run()
    value = await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}")
    assert value != ""


@pytest.mark.asyncio
async def test_transient_exception_records_retry_timestamp(state):
    sender = Sender(None, state, instantly_config(False), Env())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.side_effect = RuntimeError("boom")
    campaign = await add_campaign(state)
    await sender.run()
    value = await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}")
    assert value != ""


# ── 2. A campaign in cooldown is excluded from the deployable draft count ──


@pytest.mark.asyncio
async def test_cooldown_excludes_from_deployable_count(state):
    campaign = await add_campaign(state)
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}",
        (_utcnow() + timedelta(minutes=5)).isoformat(),
    )
    summary = await state.get_state_summary()
    assert summary["draft_campaigns"] == 1
    assert summary["deployable_draft_campaigns"] == 0


# ── 3. An expired cooldown restores eligibility ──


@pytest.mark.asyncio
async def test_expired_cooldown_restores_eligibility(state):
    campaign = await add_campaign(state)
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}",
        (_utcnow() - timedelta(minutes=1)).isoformat(),
    )
    summary = await state.get_state_summary()
    assert summary["draft_campaigns"] == 1
    assert summary["deployable_draft_campaigns"] == 1


# ── 3b. Timestamp safety ──


@pytest.mark.asyncio
async def test_timezone_aware_retry_timestamp(state):
    """A timezone-aware ISO timestamp is normalized to naive UTC for comparison."""
    campaign = await add_campaign(state)
    aware = "2026-01-01T00:00:00+00:00"  # clearly in the past
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}", aware
    )
    summary = await state.get_state_summary()
    assert summary["deployable_draft_campaigns"] == 1


@pytest.mark.asyncio
async def test_timezone_naive_retry_timestamp(state):
    """A legacy timezone-naive timestamp is compared directly as naive UTC."""
    campaign = await add_campaign(state)
    naive = (_utcnow() - timedelta(minutes=1)).isoformat()  # expired
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}", naive
    )
    summary = await state.get_state_summary()
    assert summary["deployable_draft_campaigns"] == 1


@pytest.mark.asyncio
async def test_malformed_retry_timestamp_does_not_crash(state):
    """A malformed timestamp is treated as deployable, not a crash."""
    campaign = await add_campaign(state)
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}", "not-a-timestamp"
    )
    summary = await state.get_state_summary()
    assert summary["draft_campaigns"] == 1
    assert summary["deployable_draft_campaigns"] == 1


# ── 4. decide_next_action() selects write_campaign or prospect when all drafts deferred ──


@pytest.mark.asyncio
async def test_all_drafts_deferred_picks_write_or_prospect(state):
    await add_campaign(state)
    # The campaign id is unknown here; set the cooldown with it below.
    campaign = (await state.get_campaigns_by_status("draft"))[0]
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}",
        (_utcnow() + timedelta(minutes=5)).isoformat(),
    )

    config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
        provider="instantly", require_approval=False)))

    # No writable prospects -> prospect
    summary = {"prospects": {"new": 0}, "draft_campaigns": 1,
               "deployable_draft_campaigns": 0, "writable_prospects": 0,
               "open_conversations": 0}
    assert await decide_next_action(None, None, config, summary=summary) == "prospect"

    # Writable prospects -> write_campaign
    summary["writable_prospects"] = 2
    assert await decide_next_action(None, None, config, summary=summary) == "write_campaign"


@pytest.mark.asyncio
async def test_native_provider_ignores_instantly_cooldown(state):
    """A stale Instantly cooldown must not starve native Gmail/SMTP sending."""
    await add_campaign(state)
    # Simulate a leftover Instantly cooldown after switching providers.
    campaign = (await state.get_campaigns_by_status("draft"))[0]
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}",
        (_utcnow() + timedelta(minutes=5)).isoformat(),
    )

    config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
        provider="gmail", require_approval=True)))
    summary = {"prospects": {"new": 0}, "draft_campaigns": 1,
               "deployable_draft_campaigns": 0, "writable_prospects": 0,
               "open_conversations": 0, "sending_paused": False}
    assert await decide_next_action(None, None, config, summary=summary) == "send_campaign"


@pytest.mark.asyncio
async def test_instantly_provider_defers_to_cooldown(state):
    """An Instantly provider still defers to deployable_draft_campaigns."""
    await add_campaign(state)
    campaign = (await state.get_campaigns_by_status("draft"))[0]
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}",
        (_utcnow() + timedelta(minutes=5)).isoformat(),
    )

    config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
        provider="instantly", require_approval=False)))
    summary = {"prospects": {"new": 0}, "draft_campaigns": 1,
               "deployable_draft_campaigns": 0, "writable_prospects": 0,
               "open_conversations": 0}
    assert await decide_next_action(None, None, config, summary=summary) == "prospect"


# ── 5. Sender.run() skips campaigns still in cooldown ──


@pytest.mark.asyncio
async def test_run_skips_cooldown_campaign(state):
    sender = Sender(None, state, instantly_config(False), Env())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = {"id": "remote-1"}
    campaign = await add_campaign(state)
    await state.set_setting(
        f"instantly_deploy_retry_after:{campaign.id}",
        (_utcnow() + timedelta(minutes=5)).isoformat(),
    )
    await sender.run()
    sender.instantly.create_campaign.assert_not_awaited()
    # The cooldown is still in place — nothing was cleared by the skip.
    assert await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}") != ""


# ── 6. A retry with an existing Instantly campaign ID does not call create_campaign() again ──


@pytest.mark.asyncio
async def test_retry_with_existing_id_skips_create_campaign(state):
    sender = Sender(None, state, instantly_config(False), Env())
    sender.instantly = AsyncMock()
    # First deploy: create succeeds, then set_campaign_emails fails -> cooldown set, remote id persisted.
    sender.instantly.create_campaign.return_value = {"id": "remote-1"}
    sender.instantly.set_campaign_emails.return_value = None
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="Owner", company="Acme",
        email="jane@acme.test", email_status="verified", status="new",
    ))
    campaign = await add_campaign(state, instantly_campaign_id="", prospect_ids=[pid])
    await sender.run()
    sender.instantly.create_campaign.assert_awaited_once_with("Draft")
    assert (await state.get_campaigns_by_status("draft"))[0].instantly_campaign_id == "remote-1"
    # The cooldown is now set.
    assert await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}") != ""

    # Expire the cooldown so the campaign is retried rather than skipped.
    await state.clear_instantly_retry_after(campaign.id)

    # Second deploy: all subsequent API operations succeed.
    sender.instantly.reset_mock()
    sender.instantly.create_campaign.return_value = {"id": "remote-2"}
    sender.instantly.set_campaign_emails.return_value = {}
    sender.instantly.add_leads.return_value = {}
    sender.instantly.activate_campaign.return_value = {}
    await sender.run()
    # Must NOT recreate the campaign — resume the existing remote one.
    sender.instantly.create_campaign.assert_not_awaited()
    # Deployment progresses: set_campaign_emails is called with the existing remote ID.
    sender.instantly.set_campaign_emails.assert_awaited_once()
    assert sender.instantly.set_campaign_emails.await_args.args[0] == "remote-1"
    # Leads are uploaded and activation is attempted.
    sender.instantly.add_leads.assert_awaited_once()
    sender.instantly.activate_campaign.assert_awaited_once_with("remote-1")
    # Campaign reaches the active state and the cooldown is cleared.
    active = await state.get_campaigns_by_status("active")
    assert len(active) == 1 and active[0].id == campaign.id
    assert await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}") == ""


# ── 7. Already-staged leads retry activation without being uploaded again ──


@pytest.mark.asyncio
async def test_already_staged_retry_activation_without_upload(state):
    sender = Sender(None, state, instantly_config(False), Env())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = {"id": "remote-1"}
    sender.instantly.set_campaign_emails.return_value = {}
    sender.instantly.add_leads.return_value = {}
    # First pass: add_leads marks prospects contacted, activation fails -> cooldown.
    sender.instantly.activate_campaign.return_value = None
    pid = await state.add_prospect(Prospect(
        first_name="Jane", last_name="Doe", title="Owner", company="Acme",
        email="jane@acme.test", email_status="verified", status="new",
    ))
    campaign = await add_campaign(state, instantly_campaign_id="", prospect_ids=[pid])
    await sender.run()
    sender.instantly.add_leads.assert_awaited_once()
    assert (await state.get_prospect(pid)).status == "contacted"

    # Retry: leads already staged, activation is retried without re-uploading leads.
    # Simulate cooldown expiry between cycles.
    await state.clear_instantly_retry_after(campaign.id)
    sender.instantly.reset_mock()
    sender.instantly.create_campaign.return_value = {"id": "remote-1"}
    sender.instantly.set_campaign_emails.return_value = {}
    sender.instantly.add_leads.return_value = {}
    sender.instantly.activate_campaign.return_value = {}
    await sender.run()
    sender.instantly.add_leads.assert_not_awaited()
    sender.instantly.activate_campaign.assert_awaited_once_with("remote-1")
    # Campaign is now active and its cooldown is cleared.
    active = await state.get_campaigns_by_status("active")
    assert len(active) == 1 and active[0].id == campaign.id
    assert await state.get_setting(f"instantly_deploy_retry_after:{campaign.id}") == ""
