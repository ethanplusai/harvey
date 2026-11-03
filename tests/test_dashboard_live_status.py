"""The Today tab's live status panel (/api/live-status).

Totals only count what a step can act on, so a finished step reaches 100%
instead of sticking (a business with no website is never read); a run killed
mid-way is reported as stopped rather than "running" forever; drafts and
sends are counted per person; an empty or missing database never errors.
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.signals import seed_signal_catalog
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sellbird.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "sellbird.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "sellbird.log")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    # No mailbox connected: the user sends by hand. Tests that need Sellbird
    # to be able to send set this back.
    monkeypatch.setattr(dash, "_sellbird_can_send", lambda: False)
    async def _init():
        state = StateManager(db_path=str(db))
        await state.init_db()
        await seed_signal_catalog(state)  # observations only accept known codes
    asyncio.run(_init())
    return {"db": db, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def _sql(env, query, params=()):
    with sqlite3.connect(env["db"]) as db:
        db.execute(query, params)


def _ts(minutes_ago=0):
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # the DB stores naive UTC
    return (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _company(env, cid, domain="", profiled=False, checked=False):
    _sql(env, "INSERT INTO companies (id, name, domain, profile_checked_at, "
              "prospects_checked_at) VALUES (?, ?, ?, ?, ?)",
         (cid, cid, domain, _ts(30) if profiled else None, _ts(30) if checked else None))


def _prospect(env, pid, email, email_status="verified", status="new"):
    _sql(env, "INSERT INTO prospects (id, first_name, last_name, email, email_status, "
              "status, company) VALUES (?, '', '', ?, ?, ?, 'Acme')",
         (pid, email, email_status, status))


def _campaign(env, cid, prospect_ids):
    _sql(env, "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, "
              "status) VALUES (?, ?, 'email', ?, ?, 'draft')",
         (cid, cid, json.dumps([{"step": 1, "subject": "hi", "body": "Hi {{company}}",
                                 "delay_days": 0}]), json.dumps(prospect_ids)))


def _step(data, label):
    return next(s for s in data["steps"] if s["label"] == label)


def test_live_status_on_an_empty_database(client):
    data = client.get("/api/live-status").json()
    assert data["running"] == [] and data["stopped"] == []
    assert _step(data, "Read their websites")["state"] == "waiting"
    assert data["next"] == "Search for businesses to get started."
    assert data["next_tab"] == "discover"


def test_live_status_without_a_database_file(client, env):
    env["db"].unlink()
    assert client.get("/api/live-status").status_code == 200


def test_businesses_without_a_website_do_not_stall_reading(client, env):
    _company(env, "a", "a.test", profiled=True, checked=True)
    _company(env, "b", "b.test", profiled=True, checked=True)
    _company(env, "c")  # no website: never read, so never counted as pending
    step = _step(client.get("/api/live-status").json(), "Read their websites")
    assert (step["done"], step["total"], step["state"]) == (2, 2, "done")
    assert "1 have no website (skipped)" in step["detail"]


def test_queued_drafts_are_not_counted_as_sent(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _prospect(env, "p2", "b@acme.test", status="contacted")
    _campaign(env, "c1", ["p1", "p2"])
    data = client.get("/api/live-status").json()
    sent = _step(data, "Sent")
    assert (sent["done"], sent["total"]) == (1, 2)
    assert _step(data, "Write first emails")["done"] == 2  # per person, not per campaign
    assert data["next"] == "Open Campaigns and send the 1 unsent email(s)."
    assert data["next_tab"] == "campaigns"


def test_unconfirmed_emails_are_reported_not_hidden(client, env):
    _prospect(env, "p1", "a@acme.test", email_status="verified")
    _prospect(env, "p2", "b@acme.test", email_status="guess")
    step = _step(client.get("/api/live-status").json(), "Check emails are real")
    assert (step["done"], step["total"]) == (1, 2)
    assert "1 not confirmed" in step["detail"]


def test_a_live_run_shows_progress_and_time_left(client, env):
    _sql(env, "INSERT INTO runs (id, stage, status, provider, started_at) "
              "VALUES ('r1', 'profile', 'running', 'http', ?)", (_ts(10),))
    _sql(env, "INSERT INTO companies (id, name, domain, profile_checked_at) "
              "VALUES ('a', 'a', 'a.test', ?)", (_ts(1),))
    _company(env, "b", "b.test")
    _sql(env, "INSERT INTO observations (company_id, signal_code, collector, observed_at, "
              "run_id) VALUES ('a', 'TECH_STACK', 'profile', ?, 'r1')", (_ts(1),))
    data = client.get("/api/live-status").json()
    assert [r["stage"] for r in data["running"]] == ["profile"]
    assert data["running"][0]["eta_text"].endswith("min left")
    assert _step(data, "Read their websites")["state"] == "running"
    assert data["next"].startswith("Wait") and data["next_tab"] == ""


def test_a_killed_run_is_reported_as_stopped_not_running(client, env):
    _sql(env, "INSERT INTO runs (id, stage, status, provider, started_at) "
              "VALUES ('r1', 'profile', 'running', 'http', ?)", (_ts(120),))
    _company(env, "a", "a.test")
    data = client.get("/api/live-status").json()
    assert data["running"] == []
    assert [r["stage"] for r in data["stopped"]] == ["profile"]
    assert _step(data, "Read their websites")["state"] != "running"
    assert "stopped before finishing" in data["next"]


def _outbox(env, oid, pid, status):
    _sql(env, "INSERT INTO outbox (id, prospect_id, campaign_id, step, to_email, subject, body, "
              "send_at, status) VALUES (?, ?, 'c1', 1, 'a@acme.test', 's', 'b', "
              "'2026-01-01T00:00:00', ?)", (oid, pid, status))


def test_drafts_waiting_in_the_outbox_point_there_not_at_gmail(client, env, monkeypatch):
    monkeypatch.setattr(dash, "_sellbird_can_send", lambda: True)
    # Sellbird is sending these itself: telling the user to send them from
    # Campaigns by hand would email the same person twice.
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    _outbox(env, "o1", "p1", "pending_review")
    assert client.get("/api/live-status").json()["next"] == \
        "Review the 1 draft(s) waiting in the Outbox."


def test_approved_mail_sellbird_will_send_is_not_a_job_for_the_user(client, env):
    # Even with no mailbox connected now, mail already approved is not the user's to send.
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    _outbox(env, "o1", "p1", "approved")
    assert "Open Campaigns" not in client.get("/api/live-status").json()["next"]


def test_opted_out_and_unreachable_people_are_never_asked_to_be_sent(client, env):
    _prospect(env, "p1", "a@acme.test", status="opted_out")
    _prospect(env, "p2", "b@acme.test", email_status="guess", status="queued")
    _campaign(env, "c1", ["p1", "p2"])
    assert "Open Campaigns" not in client.get("/api/live-status").json()["next"]
