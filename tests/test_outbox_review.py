"""The Outbox review desk: the facts behind each draft, and editing it.

Reviewing a cold email means checking its claims against what Harvey actually
found. The desk shows that next to the draft, and lets a reviewer fix a draft
that is nearly right instead of throwing the lead away.
"""

import asyncio
import json
import tempfile
from pathlib import Path

import aiosqlite
import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.config import DEFAULT_OPT_OUT_LINE as LINE
from harvey.models import Company
from harvey.models.campaign import Campaign
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "harvey" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


async def _seed(db_path, status="pending_review", kind="sequence"):
    state = StateManager(str(db_path))
    await state.init_db()
    await seed_signal_catalog(state)
    company_id = await state.add_company(Company(
        name="Proven Resource", domain="proven.lovable.app",
        website="https://proven.lovable.app", description="Client portal for law firms",
        industry='site:lovable.app "client portal"'))
    await state.add_observations([
        {"company_id": company_id, "signal_code": "TECH_STACK", "value_text": "Lovable, Stripe"},
        {"company_id": company_id, "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1},
    ])
    prospect_id = await state.add_prospect(Prospect(
        email="team@provenresource.com", email_status="risky", title="General inbox",
        source="scraped_mailto", company="Proven Resource", company_id=company_id,
        score=45, personalization_notes="Lovable-built client portal."))
    campaign_id = await state.add_campaign(Campaign(
        id="", name='site:lovable.app "client portal"-outreach', channel="email",
        sequence=[], prospect_ids=[prospect_id], status="active"))
    item_id = await state.add_outbox_item(
        prospect_id=prospect_id, to_email="team@provenresource.com",
        subject="your client portal", body="Hello welcome team,\n\nHaris",
        send_at="2026-01-01T00:00:00", status=status, campaign_id=campaign_id, kind=kind)
    return state, item_id


def test_pending_drafts_carry_the_facts_behind_them(client):
    asyncio.run(_seed(client.db_path))
    item = client.get("/api/outbox").json()["pending"][0]
    ctx = item["context"]
    assert ctx["company"] == "Proven Resource"
    assert ctx["website"] == "https://proven.lovable.app"
    assert ctx["description"] == "Client portal for law firms"
    assert ctx["campaign"] == 'site:lovable.app "client portal"-outreach'
    assert ctx["found_by"] == 'site:lovable.app "client portal"'
    assert ctx["contact_source"] == "scraped_mailto"
    assert ctx["email_status"] == "risky"
    assert ctx["notes"] == "Lovable-built client portal."
    assert ctx["signals"] == {"Website platform / tools": "Lovable, Stripe", "Running Google Ads": "yes"}


def test_editing_a_draft_changes_it_but_does_not_approve_it(client):
    state, item_id = asyncio.run(_seed(client.db_path))
    r = client.post(f"/api/outbox/{item_id}/edit", json={
        "subject": "your portal", "body": "Hello Proven Resource team,\n\nHaris"})
    assert r.json() == {"success": True}
    item = asyncio.run(state.get_outbox_item(item_id))
    assert item["subject"] == "your portal"
    assert item["body"] == "Hello Proven Resource team,\n\nHaris"
    assert item["status"] == "pending_review"


@pytest.mark.parametrize("status", ["approved", "sent", "rejected"])
def test_only_drafts_awaiting_review_can_be_edited(client, status):
    state, item_id = asyncio.run(_seed(client.db_path, status=status))
    r = client.post(f"/api/outbox/{item_id}/edit", json={"subject": "x", "body": "y"})
    assert r.status_code == 409
    assert asyncio.run(state.get_outbox_item(item_id))["subject"] == "your client portal"


def test_edit_needs_both_subject_and_body(client):
    _, item_id = asyncio.run(_seed(client.db_path))
    assert client.post(f"/api/outbox/{item_id}/edit",
                       json={"subject": "  ", "body": "text"}).status_code == 400
    assert client.post(f"/api/outbox/{item_id}/edit",
                       json={"subject": "s"}).status_code == 400


@pytest.mark.parametrize("body", [
    "Hi {{first_name}},\n\nQuick one.\n\nHaris",   # merge tag a person typed
    "Hi there,\n\n<a href='x'>click</a>\n\nHaris",   # HTML
])
def test_an_edit_the_sender_would_block_is_refused_with_the_reason(client, body):
    state, item_id = asyncio.run(_seed(client.db_path))
    r = client.post(f"/api/outbox/{item_id}/edit", json={"subject": "your portal", "body": body})
    assert r.status_code == 400
    assert "blocked" in r.json()["message"]
    assert asyncio.run(state.get_outbox_item(item_id))["subject"] == "your client portal"


def test_editing_needs_the_dashboard_token(client):
    _, item_id = asyncio.run(_seed(client.db_path))
    r = client.post(f"/api/outbox/{item_id}/edit", headers={"Authorization": ""},
                    json={"subject": "s", "body": "b"})
    assert r.status_code in (401, 403)


def test_a_reply_keeps_its_subject_so_it_stays_in_the_thread(client):
    state, item_id = asyncio.run(_seed(client.db_path, kind="reply"))
    r = client.post(f"/api/outbox/{item_id}/edit",
                    json={"subject": "something new", "body": "Thanks, Tuesday works."})
    assert r.json() == {"success": True}
    item = asyncio.run(state.get_outbox_item(item_id))
    assert item["subject"] == "your client portal"
    assert item["body"] == "Thanks, Tuesday works."


def test_a_reply_with_a_folded_subject_can_still_have_its_body_edited(client):
    state, item_id = asyncio.run(_seed(client.db_path, kind="reply"))

    async def fold():
        async with aiosqlite.connect(client.db_path) as db:
            await db.execute("UPDATE outbox SET subject = 'Re: your client\n portal' WHERE id = ?",
                             (item_id,))
            await db.commit()
    asyncio.run(fold())
    # The browser's <input> drops the newline; the server keeps the original.
    r = client.post(f"/api/outbox/{item_id}/edit",
                    json={"subject": "Re: your client portal", "body": "Tuesday works."})
    assert r.json() == {"success": True}
    assert asyncio.run(state.get_outbox_item(item_id))["subject"] == "Re: your client\n portal"


@pytest.mark.parametrize("payload", [[], "text", 5])
def test_a_body_that_is_not_an_object_is_a_400_not_a_crash(client, payload):
    _, item_id = asyncio.run(_seed(client.db_path))
    assert client.post(f"/api/outbox/{item_id}/edit", json=payload).status_code == 400


def test_an_edit_is_logged_with_what_it_replaced(client):
    _, item_id = asyncio.run(_seed(client.db_path))
    client.post(f"/api/outbox/{item_id}/edit", json={"subject": "your portal", "body": "New text"})
    rows = asyncio.run(dash.query_db(
        "SELECT details_json FROM actions WHERE action_type = 'outbox_edited'"))
    assert len(rows) == 1
    details = json.loads(rows[0]["details_json"])
    assert details["outbox_id"] == item_id
    assert details["old_subject"] == "your client portal"
    assert details["old_body"] == "Hello welcome team,\n\nHaris"


def test_editing_is_refused_cross_site(client):
    _, item_id = asyncio.run(_seed(client.db_path))
    r = client.post(f"/api/outbox/{item_id}/edit", json={"subject": "s", "body": "b"},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code in (401, 403)


def test_every_open_role_shows_and_unlabelled_signals_keep_their_code(client):
    state, _ = asyncio.run(_seed(client.db_path))

    async def more():
        cid = (await dash.query_db("SELECT id FROM companies"))[0]["id"]
        await state.add_observations([
            {"company_id": cid, "signal_code": "HIRING_ROLE", "value_text": "Roofing estimator",
             "run_id": "r1"},
            {"company_id": cid, "signal_code": "HIRING_ROLE", "value_text": "Crew foreman",
             "run_id": "r1"},
        ])
        async with aiosqlite.connect(client.db_path) as db:
            await db.execute("UPDATE signal_codes SET label = '' WHERE code = 'TECH_STACK'")
            await db.commit()
    asyncio.run(more())
    signals = client.get("/api/outbox").json()["pending"][0]["context"]["signals"]
    assert signals["TECH_STACK"] == "Lovable, Stripe"
    assert "" not in signals
    hiring = [v for k, v in signals.items() if "hiring" in k.lower() or k == "HIRING_ROLE"]
    assert len(hiring) == 1
    assert "Roofing estimator" in hiring[0] and "Crew foreman" in hiring[0]


def test_an_approved_edit_is_what_gets_sent(client):
    from tests.test_outbox_native import FakeProvider, make_sender
    state, item_id = asyncio.run(_seed(client.db_path))
    client.post(f"/api/outbox/{item_id}/edit",
                json={"subject": "your portal", "body": "Hello Proven Resource team,\n\nHaris"})
    assert client.post(f"/api/outbox/{item_id}/approve").json()["success"]

    async def send():
        async with aiosqlite.connect(client.db_path) as db:
            await db.execute("UPDATE prospects SET email_status = 'verified'")
            await db.commit()
        provider = FakeProvider()
        await make_sender(state, provider)._drain_due()
        return provider.sent
    sent = asyncio.run(send())
    # The edit as saved, with the compliance footer the Sender adds underneath.
    assert [(m["subject"], m["body"].split("\n\n" + LINE)[0]) for m in sent] == [
        ("your portal", "Hello Proven Resource team,\n\nHaris")]


def test_a_closed_role_shows_as_not_hiring_to_the_reviewer(client):
    state, _ = asyncio.run(_seed(client.db_path))

    async def more():
        cid = (await dash.query_db("SELECT id FROM companies"))[0]["id"]
        await state.add_observations([
            {"company_id": cid, "signal_code": "HIRING_ROLE", "value_text": "Head of Growth",
             "run_id": "r1", "observed_at": "2026-09-01 10:00:00"},
            # The next profile read their board: nothing relevant open now.
            {"company_id": cid, "signal_code": "HIRING_ROLE", "value_num": 0, "value_text": "",
             "run_id": "r2", "observed_at": "2026-10-01 10:00:00"},
        ])
    asyncio.run(more())
    signals = client.get("/api/outbox").json()["pending"][0]["context"]["signals"]
    hiring = [v for k, v in signals.items() if "hiring" in k.lower() or k == "HIRING_ROLE"]
    assert hiring == ["no"]
