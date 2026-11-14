"""The cleanup sweep: taking the wrong companies out of the pipeline.

Three kinds of company must never be a prospect: a page that is not a
company at all, a company that sells software rather than making things,
and a company that trades goods rather than making them. These tests pin
that the sweep finds them, takes them out, and leaves everything else —
and every real contact — exactly where it was.
"""

import os
import tempfile

import pytest
import pytest_asyncio

from sellbird.cleanup import audit, cleanup
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "t.db"))
        await sm.init_db()
        yield sm


async def _company(state, name, domain, industry, source_url=""):
    return await state.add_company(Company(
        name=name, domain=domain, industry=industry, source_url=source_url,
        description="", location="Pune"))


async def _prospect(state, name, email, company_id):
    return await state.add_prospect(Prospect(
        first_name="Pat", last_name="Shah", title="Owner", company=name,
        industry="polymer manufacturer", email=email, email_status="verified",
        company_id=company_id))


@pytest.mark.asyncio
async def test_the_audit_finds_the_wrong_companies(state):
    good = await _company(state, "Acme Polymers", "acme.test",
                          "polymer manufacturer")
    await _company(state, "Delta Sales App", "deltasalesapp.test", "Software")
    await _company(state, "M Y Trading Co.", "mytrading.test", "trading company")
    await _company(state, "Manufacturing Companies in Nagpur", "nagpuronline.test",
                   "Manufacturing", "https://nagpuronline.in/guide/manufacturing-companies")
    await _prospect(state, "Acme Polymers", "a@acme.test", good)

    report = await audit(state)
    assert report["total"] == 4
    assert report["excluded_count"] == 3
    names = {r["name"] for r in report["excluded"]}
    assert "Acme Polymers" not in names
    assert names == {"Delta Sales App", "M Y Trading Co.",
                     "Manufacturing Companies in Nagpur"}


@pytest.mark.asyncio
async def test_the_cleanup_takes_the_company_and_its_prospect_out(state):
    vendor = await _company(state, "Delta Sales App", "deltasalesapp.test",
                            "Software")
    pid = await _prospect(state, "Delta Sales App", "info@deltasalesapp.test",
                          vendor)

    report = await cleanup(state)
    assert report["excluded"] == 1
    assert report["prospects"] == 1

    company = await state.get_company_by_domain("deltasalesapp.test")
    assert company.status == "excluded"
    assert company.excluded_reason

    prospect = await state.get_prospect(pid)
    assert prospect.status == "excluded"


@pytest.mark.asyncio
async def test_the_cleanup_cancels_a_draft_waiting_for_approval(state):
    vendor = await _company(state, "Delta Sales App", "deltasalesapp.test",
                            "Software")
    pid = await _prospect(state, "Delta Sales App", "info@deltasalesapp.test",
                          vendor)
    outbox_id = await state.add_outbox_item(
        prospect_id=pid,
        to_email="info@deltasalesapp.test",
        subject="quick question",
        body="Hi, worth a chat?",
        send_at="2026-01-01T00:00:00",
        kind="sequence",
        step=1,
        status="pending_review",
    )

    report = await cleanup(state)
    assert report["cancelled_drafts"] == 1

    items = await state.get_outbox(status="pending_review")
    assert outbox_id not in {i["id"] for i in items}


@pytest.mark.asyncio
async def test_a_good_company_is_left_alone(state):
    good = await _company(state, "Acme Polymers", "acme.test",
                          "polymer manufacturer")
    pid = await _prospect(state, "Acme Polymers", "a@acme.test", good)

    await cleanup(state)
    company = await state.get_company_by_domain("acme.test")
    assert company.status == "active"
    prospect = await state.get_prospect(pid)
    assert prospect.status == "new"


@pytest.mark.asyncio
async def test_a_dry_run_changes_nothing(state):
    await _company(state, "Delta Sales App", "deltasalesapp.test", "Software")

    report = await cleanup(state, dry_run=True)
    assert report["excluded"] == 1
    company = await state.get_company_by_domain("deltasalesapp.test")
    assert company.status == "active"


@pytest.mark.asyncio
async def test_the_sweep_can_leave_trading_houses_on_file(state):
    await _company(state, "Delta Sales App", "deltasalesapp.test", "Software")
    await _company(state, "M Y Trading Co.", "mytrading.test", "trading company")

    report = await cleanup(state, include_retail=False)
    assert report["excluded"] == 1
    company = await state.get_company_by_domain("mytrading.test")
    assert company.status == "active"


@pytest.mark.asyncio
async def test_an_excluded_company_can_be_put_back(state):
    good = await _company(state, "Acme Polymers", "acme.test",
                          "polymer manufacturer")
    pid = await _prospect(state, "Acme Polymers", "a@acme.test", good)

    await state.exclude_company(good, "trading / retail, not manufacturing")
    assert (await state.get_prospect(pid)).status == "excluded"

    await state.restore_company(good)
    assert (await state.get_company_by_domain("acme.test")).status == "active"
    assert (await state.get_prospect(pid)).status == "new"


@pytest.mark.asyncio
async def test_an_excluded_company_is_not_offered_to_the_writer(state):
    """The marker is the last line of defence.

    A company whose name looks fine still stays out once the operator has
    excluded it.
    """
    vendor = await _company(state, "Acme Polymers", "acmepolymers.test",
                            "polymer manufacturer")
    await _prospect(state, "Acme Polymers", "a@acmepolymers.test", vendor)

    # Not caught by name — the marker is what stops it.
    assert await state.get_prospects_to_write() != []

    await state.exclude_company(vendor, "operator decision")
    assert await state.get_prospects_to_write() == []


@pytest.mark.asyncio
async def test_the_sweep_is_idempotent(state):
    await _company(state, "Delta Sales App", "deltasalesapp.test", "Software")

    first = await cleanup(state)
    second = await cleanup(state)
    assert first["excluded"] == 1
    assert second["excluded"] == 0
