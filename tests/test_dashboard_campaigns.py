"""The Campaigns tab's list: sorted and paged in SQL, empty campaigns left
out unless asked for, where each campaign's first emails stand in the
Outbox, and why a draft isn't moving. Every business here is invented."""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
STEPS = [{"step": 1, "subject": "hi", "body": "Hi {{company}}", "delay_days": 0},
         {"step": 2, "subject": "re: hi", "body": "One more thing", "delay_days": 4}]
CAMPAIGNS = [  # id, name, status, people, created
    ("c1", "proptech-outreach", "active", ["p0", "p1", "p2", "p3"], "2026-10-20 09:00:00"),
    ("c2", "healthtech-outreach", "draft", ["p4"], "2026-10-25 09:00:00"),
    ("c3", "Ébénisterie-outreach", "draft", ["p5", "p6"], "2026-10-22 09:00:00"),
    ("c4", "zeta-outreach", "draft", [], "2026-10-30 09:00:00"),
]
FIRST_EMAILS = [("c1", "p0", "pending_review"), ("c1", "p1", "pending_review"),
                ("c1", "p2", "approved"), ("c1", "p3", "sent"), ("c3", "p5", "pending_review")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "_harvey_can_send", lambda: True)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    state = StateManager(db_path=str(db))

    async def _seed():
        await state.init_db()
        for cid, pid, status in FIRST_EMAILS:
            await state.add_outbox_item(
                prospect_id=pid, campaign_id=cid, step=1, to_email=f"{pid}@lumenhexa.example",
                subject="hi", body="Hi", send_at="2026-11-01T09:00:00", status=status)
        # A follow-up isn't a first email.
        await state.add_outbox_item(
            prospect_id="p0", campaign_id="c1", step=2, to_email="p0@lumenhexa.example",
            subject="re: hi", body="More", send_at="2026-11-05T09:00:00")
    asyncio.run(_seed())
    with sqlite3.connect(db) as c:
        for cid, name, status, people, created in CAMPAIGNS:
            c.execute("INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, "
                      "status, created_at) VALUES (?, ?, 'email', ?, ?, ?, ?)",
                      (cid, name, json.dumps(STEPS), json.dumps(people), status, created))
    return {"db": db, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def ids(client, **params):
    return [c["id"] for c in client.get("/api/campaigns", params=params).json()["items"]]


def _by_id(client):
    return {c["id"]: c for c in client.get("/api/campaigns", params={"empty": 1}).json()["items"]}


# ── Sort, pages, empty campaigns ──

def test_newest_first_without_the_empty_ones(client):
    page = client.get("/api/campaigns").json()
    assert [c["id"] for c in page["items"]] == ["c2", "c3", "c1"]
    assert (page["total"], page["empty"]) == (3, 1)


def test_empty_campaigns_when_asked_for(client):
    page = client.get("/api/campaigns", params={"empty": 1}).json()
    assert [c["id"] for c in page["items"]] == ["c4", "c2", "c3", "c1"]
    assert (page["total"], page["empty"]) == (4, 1)


def test_sort_by_each_column(client):
    # Accents don't push Ébénisterie after proptech.
    assert ids(client, sort="name", dir="asc") == ["c3", "c2", "c1"]
    assert ids(client, sort="people", dir="desc") == ["c1", "c3", "c2"]
    # First emails: the most waiting for review first.
    assert ids(client, sort="first_emails", dir="desc") == ["c1", "c3", "c2"]
    # Drafts before scheduled campaigns; ties newest first.
    assert ids(client, sort="status", dir="asc") == ["c2", "c3", "c1"]
    assert ids(client, sort="created", dir="asc") == ["c1", "c3", "c2"]


def test_unknown_sort_falls_back_to_newest(client):
    assert ids(client, sort="name; DROP TABLE campaigns") == ids(client)


def test_pages(client):
    page = client.get("/api/campaigns", params={"limit": 2, "offset": 2}).json()
    assert [c["id"] for c in page["items"]] == ["c1"]
    assert (page["total"], page["offset"], page["limit"]) == (3, 2, 2)
    assert client.get("/api/campaigns", params={"limit": 999}).json()["limit"] == 100


# ── What each row says ──

def test_first_emails_are_counted_by_status(client):
    rows = _by_id(client)
    assert rows["c1"]["first_emails"] == {"pending_review": 2, "approved": 1, "sent": 1}
    assert rows["c3"]["first_emails"] == {"pending_review": 1}
    assert rows["c2"]["first_emails"] == {} and rows["c4"]["first_emails"] == {}


def test_a_draft_says_sending_is_paused(client, env):
    assert all(not c["held"] for c in _by_id(client).values())
    with sqlite3.connect(env["db"]) as c:
        c.execute("INSERT INTO settings (key, value) VALUES ('sending_paused', 'paused manually')")
    rows = _by_id(client)
    assert rows["c2"]["held"] == "paused manually" and rows["c4"]["held"] == "paused manually"
    assert rows["c1"]["held"] == ""          # already scheduled: nothing to hold


def test_resuming_clears_the_reason(client, env):
    with sqlite3.connect(env["db"]) as c:
        c.execute("INSERT INTO settings (key, value) VALUES ('sending_paused', '')")
    assert _by_id(client)["c2"]["held"] == ""
