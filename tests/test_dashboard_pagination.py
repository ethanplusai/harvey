"""Every growing list on the dashboard is paged by the server and says how
much it holds: Activity, Conversations and the Outbox's four lists (Companies,
Contacts and Campaigns have their own tests). Every business here is invented."""

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.state import HELD_FOR_REVIEW, StateManager

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    asyncio.run(StateManager(db_path=str(db)).init_db())
    return {"db": db, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def _sql(env, query, rows):
    with sqlite3.connect(env["db"]) as c:
        c.executemany(query, rows)


# ── Activity ──

def _actions(env, n, agent="scout"):
    _sql(env, "INSERT INTO actions (id, action_type, agent, created_at) VALUES (?, 'prospect', ?, ?)",
         [(f"{agent}{i}", agent, f"2026-11-01 {i // 60:02d}:{i % 60:02d}:00") for i in range(n)])


def test_activity_pages_newest_first(client, env):
    _actions(env, 60)
    first = client.get("/api/activity").json()
    assert (first["total"], first["offset"], first["limit"], len(first["items"])) == (60, 0, 50, 50)
    assert first["items"][0]["id"] == "scout59"
    rest = client.get("/api/activity", params={"offset": 50}).json()
    assert [a["id"] for a in rest["items"]][-1] == "scout0" and len(rest["items"]) == 10
    assert client.get("/api/activity", params={"limit": 999}).json()["limit"] == 200


def test_activity_without_heartbeats(client, env):
    _actions(env, 3)
    _actions(env, 5, agent="main")
    assert client.get("/api/activity").json()["total"] == 8
    quiet = client.get("/api/activity", params={"heartbeats": "false"}).json()
    assert quiet["total"] == 3 and {a["agent"] for a in quiet["items"]} == {"scout"}


# ── Conversations ──

def _conversations(env, status, n, start=0):
    _sql(env, "INSERT INTO conversations (id, prospect_id, status, updated_at) VALUES (?, ?, ?, ?)",
         [(f"{status}{i}", f"p-{status}{i}", status, f"2026-11-01 10:{(start + i):02d}:00")
          for i in range(n)])


def test_conversations_page_under_a_filter_with_counts(client, env):
    _conversations(env, "open", 25)
    _conversations(env, "closed", 3)
    data = client.get("/api/conversations").json()
    # Nothing needs you, so it opens on "open".
    assert data["filter"] == "open" and data["total"] == 25 and len(data["items"]) == 20
    assert data["counts"] == {"needs": 0, "open": 25, "awaiting": 0, "closed": 3, "all": 28}
    assert data["items"][0]["id"] == "open24"                      # newest first
    rest = client.get("/api/conversations", params={"filter": "open", "offset": 20}).json()
    assert len(rest["items"]) == 5 and rest["items"][-1]["id"] == "open0"
    closed = client.get("/api/conversations", params={"filter": "closed"}).json()
    assert {c["id"] for c in closed["items"]} == {"closed0", "closed1", "closed2"}


def test_what_needs_you_comes_first_and_is_where_the_tab_opens(client, env):
    _conversations(env, "open", 4)
    _conversations(env, "needs_human", 1, start=50)
    data = client.get("/api/conversations").json()
    assert data["filter"] == "needs" and [c["id"] for c in data["items"]] == ["needs_human0"]
    every = client.get("/api/conversations", params={"filter": "all"}).json()["items"]
    assert every[0]["id"] == "needs_human0" and len(every) == 5


# ── Outbox ──

def _outbox(env, status, n, error="", tag=""):
    _sql(env, "INSERT INTO outbox (id, prospect_id, to_email, subject, body, send_at, status, error, "
              "sent_at, updated_at) VALUES (?, '', ?, 'hi', 'Hi', ?, ?, ?, ?, ?)",
         [(f"{status}{tag}{i}", f"{status}{tag}{i}@lumenhexa.example", f"2026-11-02T09:{i:02d}:00", status, error,
           f"2026-11-01T09:{i:02d}:00", f"2026-11-01T09:{i:02d}:00") for i in range(n)])


def test_each_outbox_list_is_paged_with_its_total(client, env):
    _outbox(env, "pending_review", 58)
    _outbox(env, "pending_review", 2, error=HELD_FOR_REVIEW + "a link to another site", tag="held")
    _outbox(env, "approved", 30)
    # Claimed just now: a send cut off long ago would count as failed.
    _sql(env, "UPDATE outbox SET status = 'sending', updated_at = ? WHERE id = ?",
         [(datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), "approved29")])
    _outbox(env, "sent", 30)
    _outbox(env, "rejected", 3)
    data = client.get("/api/outbox").json()
    pages = data["pages"]
    assert {k: (v["total"], v["limit"]) for k, v in pages.items()} == {
        "pending": (60, 50), "approved": (30, 25), "sent": (30, 25), "failed": (3, 25)}
    assert len(data["pending"]) == 50 and len(data["approved"]) == 25 and data["held"] == 2
    assert data["approved"][0]["id"] == "approved29"              # going out now leads
    assert data["sent"][0]["id"] == "sent29"                      # newest sent first
    more = client.get("/api/outbox", params={"pending_offset": 50, "approved_offset": 25,
                                             "sent_offset": 25}).json()
    assert (len(more["pending"]), len(more["approved"]), len(more["sent"])) == (10, 5, 5)


def test_a_page_past_the_end_falls_back_to_the_last(client, env):
    _outbox(env, "sent", 30)
    data = client.get("/api/outbox", params={"sent_offset": 500}).json()
    assert data["pages"]["sent"]["offset"] == 25 and len(data["sent"]) == 5
