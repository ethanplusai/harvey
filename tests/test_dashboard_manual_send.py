"""Sending by hand from the dashboard.

Covers the Campaigns tab's "Open in Gmail" data (recipients per campaign) and
the "Mark as sent" toggle (never erases a reply, refuses unknown prospects).
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
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
    asyncio.run(StateManager(db_path=str(db)).init_db())
    return {"db": db, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def _sql(env, query, params=()):
    with sqlite3.connect(env["db"]) as db:
        db.execute(query, params)


def _prospect(env, pid, email, email_status="verified", status="new"):
    _sql(env, "INSERT INTO prospects (id, first_name, last_name, email, email_status, "
              "status, company) VALUES (?, '', '', ?, ?, ?, 'Acme')",
         (pid, email, email_status, status))


def _campaign(env, cid, prospect_ids):
    _sql(env, "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, "
              "status) VALUES (?, ?, 'email', ?, ?, 'draft')",
         (cid, cid, json.dumps([{"step": 1, "subject": "hi", "body": "Hi {{company}}",
                                 "delay_days": 0}]), json.dumps(prospect_ids)))


# ── Campaign recipients ──


def test_campaigns_list_their_recipients(client, env):
    _prospect(env, "p1", "a@acme.test")
    _prospect(env, "p2", "")  # no address: nothing to open in Gmail
    _campaign(env, "c1", ["p1", "p2"])
    rows = client.get("/api/campaigns").json()
    assert [r["email"] for r in rows[0]["recipients"]] == ["a@acme.test"]
    assert rows[0]["recipients"][0]["status"] == "new"


def test_campaign_with_no_prospects_has_no_recipients(client, env):
    _campaign(env, "c1", [])
    assert client.get("/api/campaigns").json()[0]["recipients"] == []


# ── Mark as sent ──


def test_mark_sent_and_undo(client, env):
    _prospect(env, "p1", "a@acme.test")
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).json()["sent"] is True
    assert client.get("/api/campaigns").json() == []  # no campaign needed to toggle
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT status FROM prospects").fetchone()[0] == "contacted"
        assert db.execute("SELECT action_type FROM actions").fetchone()[0] == "manual_send"
    client.post("/api/prospects/p1/sent", json={"sent": False})
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT status FROM prospects").fetchone()[0] == "new"


def test_unticking_never_erases_a_reply(client, env):
    _prospect(env, "p1", "a@acme.test", status="replied")
    r = client.post("/api/prospects/p1/sent", json={"sent": False}).json()
    assert r == {"success": True, "sent": True, "status": "replied"}
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT status FROM prospects").fetchone()[0] == "replied"


def test_unknown_prospect_is_refused(client):
    assert client.post("/api/prospects/nope/sent", json={"sent": True}).status_code == 404


def test_missing_body_means_sent(client, env):
    _prospect(env, "p1", "a@acme.test")
    assert client.post("/api/prospects/p1/sent").json()["sent"] is True


# ── Never email someone twice ──


def _outbox(env, oid, pid, status, step=1, campaign="c1"):
    _sql(env, "INSERT INTO outbox (id, prospect_id, campaign_id, step, to_email, subject, body, "
              "send_at, status) VALUES (?, ?, ?, ?, 'a@acme.test', 's', 'b', "
              "'2026-01-01T00:00:00', ?)", (oid, pid, campaign, step, status))


def _status(env, table, row_id):
    with sqlite3.connect(env["db"]) as db:
        return db.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


def test_sending_by_hand_cancels_what_sellbird_had_queued(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _outbox(env, "o1", "p1", "pending_review", step=1)
    _outbox(env, "o2", "p1", "pending_review", step=2)
    client.post("/api/prospects/p1/sent", json={"sent": True})
    assert _status(env, "outbox", "o1") == "cancelled"
    assert _status(env, "outbox", "o2") == "cancelled"


def test_undo_puts_the_prospect_and_their_drafts_back_for_review(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _outbox(env, "o1", "p1", "pending_review")
    client.post("/api/prospects/p1/sent", json={"sent": True})
    assert _status(env, "outbox", "o1") == "cancelled"
    assert client.post("/api/prospects/p1/sent", json={"sent": False}).json()["sent"] is False
    assert _status(env, "prospects", "p1") == "queued"   # not 'new': the Writer won't redraft
    assert _status(env, "outbox", "o1") == "pending_review"  # back for review


def test_undo_never_cancels_an_unrelated_cancellation(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _outbox(env, "o1", "p1", "cancelled")
    client.post("/api/prospects/p1/sent", json={"sent": True})
    client.post("/api/prospects/p1/sent", json={"sent": False})
    assert _status(env, "outbox", "o1") == "cancelled"


def test_unticking_someone_sellbird_emailed_is_refused(client, env):
    _prospect(env, "p1", "a@acme.test", status="contacted")
    _outbox(env, "o1", "p1", "sent")
    r = client.post("/api/prospects/p1/sent", json={"sent": False})
    assert r.status_code == 409
    assert _status(env, "prospects", "p1") == "contacted"


def test_unticking_someone_sent_through_instantly_is_refused(client, env):
    _prospect(env, "p1", "a@acme.test", status="contacted")
    _campaign(env, "c1", ["p1"])
    _sql(env, "UPDATE campaigns SET instantly_campaign_id = 'inst-1', status = 'active'")
    assert client.post("/api/prospects/p1/sent", json={"sent": False}).status_code == 409
    assert _status(env, "prospects", "p1") == "contacted"


def test_recipients_say_who_can_be_sent_by_hand(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _prospect(env, "p2", "b@acme.test", status="queued")              # Sellbird approved mail
    _prospect(env, "p3", "c@acme.test", status="opted_out")           # asked to be removed
    _prospect(env, "p4", "d@acme.test", email_status="guess")         # undeliverable
    _prospect(env, "p5", "e@acme.test", email_status="invalid")
    _prospect(env, "p6", "f@acme.test", email_status="risky")         # catch-all: allowed
    _campaign(env, "c1", ["p1", "p2", "p3", "p4", "p5", "p6"])
    _outbox(env, "o1", "p2", "approved")
    recips = {r["id"]: r for r in client.get("/api/campaigns").json()[0]["recipients"]}
    assert {k for k, r in recips.items() if r["can_send_by_hand"]} == {"p1", "p6"}


def test_no_gmail_button_while_sellbird_can_send_but_a_hand_send_can_still_be_recorded(
        client, env, monkeypatch):
    # Recording "I already emailed them" can only stop Sellbird sending, so it
    # is never refused just because a mailbox is connected now.
    monkeypatch.setattr(dash, "_sellbird_can_send", lambda: True)
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    _outbox(env, "o1", "p1", "pending_review")
    r = client.get("/api/campaigns").json()[0]["recipients"][0]
    assert r["can_send_by_hand"] is False and r["can_mark_sent"] is True
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).json()["sent"] is True
    assert _status(env, "prospects", "p1") == "contacted"
    assert _status(env, "outbox", "o1") == "cancelled"


def test_someone_with_approved_mail_cant_be_marked_sent(client, env):
    # Approved mail may be mid-send right now: the Outbox is where to stop it.
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    _outbox(env, "o1", "p1", "approved")
    assert client.get("/api/campaigns").json()[0]["recipients"][0]["can_mark_sent"] is False
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).status_code == 409
    assert _status(env, "outbox", "o1") == "approved"


def test_someone_whose_mail_is_being_sent_cant_be_marked_sent(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    _outbox(env, "o1", "p1", "sending")
    assert client.get("/api/campaigns").json()[0]["recipients"][0]["can_mark_sent"] is False
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).status_code == 409
    assert _status(env, "outbox", "o1") == "sending"


def test_mail_that_may_have_gone_out_counts_as_sent_by_sellbird(client, env):
    # A send cut off mid-way may have reached them: never offer a second one.
    from sellbird.state import MAYBE_SENT_PREFIX
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    _outbox(env, "o1", "p1", "failed")
    _sql(env, "UPDATE outbox SET error = ? WHERE id = 'o1'", (MAYBE_SENT_PREFIX + "timeout",))
    recipient = client.get("/api/campaigns").json()[0]["recipients"][0]
    assert recipient["can_mark_sent"] is False and recipient["can_send_by_hand"] is False
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).status_code == 409


def test_mail_that_plainly_failed_doesnt_block_a_hand_send(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    _outbox(env, "o1", "p1", "failed")
    _sql(env, "UPDATE outbox SET error = 'gate: unrendered merge tag' WHERE id = 'o1'")
    assert client.get("/api/campaigns").json()[0]["recipients"][0]["can_send_by_hand"] is True


def test_the_outbox_tab_shows_mail_being_sent_and_surfaces_stuck_mail(client, env):
    _prospect(env, "p1", "a@acme.test", status="contacted")
    _outbox(env, "o1", "p1", "sending")
    _sql(env, "UPDATE outbox SET updated_at = ? WHERE id = 'o1'",
         (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),))
    _outbox(env, "o2", "p1", "sending", step=2)
    _sql(env, "UPDATE outbox SET updated_at = '2026-01-01T00:00:00' WHERE id = 'o2'")
    data = client.get("/api/outbox").json()
    assert [i["id"] for i in data["approved"]] == ["o1"]
    assert [i["id"] for i in data["failed"]] == ["o2"]


def test_rejecting_mail_already_on_its_way_is_refused(client, env):
    _prospect(env, "p1", "a@acme.test", status="contacted")
    _outbox(env, "o1", "p1", "sending")
    _outbox(env, "o2", "p1", "sent", step=2)
    assert client.post("/api/outbox/o1/reject").status_code == 409
    assert client.post("/api/outbox/o2/reject").status_code == 409
    assert _status(env, "outbox", "o1") == "sending"
    assert _status(env, "outbox", "o2") == "sent"


def test_drafts_awaiting_review_dont_hide_the_gmail_button_when_sellbird_cant_send(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    _outbox(env, "o1", "p1", "pending_review")   # staged, but no mailbox to send it
    assert client.get("/api/campaigns").json()[0]["recipients"][0]["can_send_by_hand"] is True


def test_a_hand_sent_prospect_is_marked_for_the_sender(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    client.post("/api/prospects/p1/sent", json={"sent": True})
    r = client.get("/api/campaigns").json()[0]["recipients"][0]
    assert r["sent_by_hand"] is True and r["can_send_by_hand"] is False
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT value FROM settings WHERE key = 'manual_send:p1'").fetchone()[0] == "queued"
    client.post("/api/prospects/p1/sent", json={"sent": False})
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT 1 FROM settings WHERE key = 'manual_send:p1'").fetchone() is None


def test_ticking_someone_sellbird_emailed_changes_nothing(client, env):
    _prospect(env, "p1", "a@acme.test", status="contacted")
    _outbox(env, "o1", "p1", "sent", step=1)
    _outbox(env, "o2", "p1", "approved", step=2)   # Sellbird's follow-up
    client.post("/api/prospects/p1/sent", json={"sent": True})
    assert _status(env, "outbox", "o2") == "approved"


def test_undo_after_a_reply_keeps_the_reply(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    client.post("/api/prospects/p1/sent", json={"sent": True})
    _sql(env, "UPDATE prospects SET status = 'replied'")
    client.post("/api/prospects/p1/sent", json={"sent": False})
    assert _status(env, "prospects", "p1") == "replied"


@pytest.mark.parametrize("payload", [[], "x", {"sent": "false"}])
def test_a_body_that_is_not_an_object_is_refused(client, env, payload):
    _prospect(env, "p1", "a@acme.test")
    assert client.post("/api/prospects/p1/sent", json=payload).status_code == 400
    assert _status(env, "prospects", "p1") == "new"


# ── When is sending by hand offered at all? ──


class _Email:
    def __init__(self, enabled=True, provider="smtp", approval=False):
        self.enabled, self.provider, self.require_approval = enabled, provider, approval


class _Cfg:
    def __init__(self, **kw):
        self.channels = type("C", (), {"email": _Email(**kw)})()


class _Env:
    def __init__(self, instantly=""):
        self.instantly_api_key = instantly


class _Provider:
    def __init__(self, ok):
        self.ok = ok

    def is_configured(self):
        return self.ok


@pytest.mark.parametrize("cfg, env, provider_ok, expected", [
    (_Cfg(enabled=False), _Env(), True, False),                 # email off: hand sends only
    (_Cfg(provider="smtp"), _Env(), False, False),              # mailbox not connected
    (_Cfg(provider="gmail"), _Env(), True, True),               # connected: Sellbird sends
    (_Cfg(provider="instantly"), _Env(instantly="k"), True, True),
    (_Cfg(provider="instantly"), _Env(), True, False),
    (_Cfg(provider="instantly", approval=True), _Env(instantly="k"), True, False),  # held
])
def test_sellbird_can_send(monkeypatch, cfg, env, provider_ok, expected):
    import sellbird.config
    import sellbird.integrations.mail_provider as mp
    monkeypatch.setattr(sellbird.config, "load_config", lambda: cfg)
    monkeypatch.setattr(sellbird.config, "load_env", lambda: env)
    monkeypatch.setattr(mp, "get_mail_provider", lambda c, e: _Provider(provider_ok))
    # These tests don't use the env fixture, so this is the real check.
    assert dash._sellbird_can_send() is expected


def test_an_unreadable_config_means_sellbird_might_send(monkeypatch):
    import sellbird.config

    def boom():
        raise ValueError("bad yaml")
    monkeypatch.setattr(sellbird.config, "load_config", boom)
    assert dash._sellbird_can_send() is True


def test_marking_twice_says_it_was_already_marked(client, env):
    # A double click must not open a second ready-to-send Gmail draft.
    _prospect(env, "p1", "a@acme.test", status="queued")
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).json() == {
        "success": True, "sent": True}
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).json()["already"] is True


def test_mail_approved_while_marking_is_refused_and_the_marker_removed(client, env, monkeypatch):
    # The marker goes down first; if mail got approved meanwhile, the drain
    # may already hold it, so the mark is refused and undone.
    _prospect(env, "p1", "a@acme.test", status="queued")
    real = dash._sellbird_committed_ids

    async def approved_meanwhile():
        _outbox(env, "o1", "p1", "approved")
        with sqlite3.connect(env["db"]) as db:
            assert db.execute("SELECT 1 FROM settings WHERE key = 'manual_send:p1'").fetchone()
        return await real()
    monkeypatch.setattr(dash, "_sellbird_committed_ids", approved_meanwhile)
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).status_code == 409
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT 1 FROM settings WHERE key = 'manual_send:p1'").fetchone() is None
    assert _status(env, "prospects", "p1") == "queued"
