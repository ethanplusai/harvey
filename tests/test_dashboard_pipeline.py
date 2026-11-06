"""/api/stats pipeline: one funnel stage per contact. Prospect status stops at
"replied"; how the reply went comes from the latest conversation."""

import asyncio
import sqlite3

from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from sellbird.state import StateManager


def _sql(db, query, params=()):
    with sqlite3.connect(db) as conn:
        conn.execute(query, params)
    conn.close()


def test_pipeline_buckets_each_contact_once(client):
    db = client.db_path
    asyncio.run(StateManager(str(db)).init_db())
    people = [("a", "new"), ("b", "queued"), ("c", "contacted"), ("d", "replied"), ("e", "replied"),
              ("f", "replied"), ("g", "replied"), ("h", "lost"), ("i", "opted_out"), ("j", "replied"), ("k", "contacted")]
    for pid, status in people:
        _sql(db, "INSERT INTO prospects (id, first_name, status) VALUES (?, 'X', ?)", (pid, status))
    _sql(db, "UPDATE prospects SET email_status = 'invalid' WHERE id = 'k'")   # bounced
    convos = [
        ("c1", "d", "question", "engaged", "2026-10-01"),
        ("c2", "e", "interested", "engaged", "2026-10-01"),
        ("c3", "f", "question", "closing", "2026-10-01"),
        ("c4", "g", "objection", "presenting", "2026-10-01"),
        # j: an old interested thread, then a not-interested one: the latest wins.
        ("c5", "j", "interested", "closing", "2026-10-01"),
        ("c6", "j", "not_interested", "closed_lost", "2026-10-05"),
    ]
    for cid, pid, intent, stage, at in convos:
        _sql(db, "INSERT INTO conversations (id, prospect_id, intent, stage, updated_at) VALUES (?,?,?,?,?)",
             (cid, pid, intent, stage, at))
    pipe = client.get("/api/stats").json()["pipeline"]
    assert pipe == {"to_write": 2, "contacted": 1, "replied": 1, "interested": 2,
                    "ready_to_meet": 1, "closed_out": 4}
    assert sum(pipe.values()) == len(people)


def test_discover_tab_knows_how_many_sites_are_unread(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    data = client.get("/api/discover/providers").json()
    assert data["unprofiled"] == 0 and data["running_kind"] in ("discover", "profile")


def test_a_paused_brain_is_on_today_not_hidden(client, monkeypatch):
    import json as _json
    import sellbird.dashboard as dash
    asyncio.run(StateManager(str(client.db_path)).init_db())
    monkeypatch.setattr(dash, "_active_provider", lambda: "codex")
    _sql(client.db_path, "INSERT INTO settings (key, value) VALUES (?, ?)",
         (__import__("sellbird.brain", fromlist=["x"]).BRAIN_STATUS_KEY,
          _json.dumps({"provider": "codex", "status": "paused", "reason": "codex login expired"})))
    items = client.get("/api/today").json()["items"]
    [item] = [i for i in items if i["key"] == "brain-paused"]
    assert "codex login expired" in item["detail"] and item["tone"] == "bad"


def test_a_run_that_went_quiet_is_not_shown_as_running(client):
    from datetime import datetime, timedelta, timezone
    asyncio.run(StateManager(str(client.db_path)).init_db())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _sql(client.db_path, "INSERT INTO runs (id, stage, status, started_at) VALUES ('old', 'profile', 'running', ?)",
         ((now - timedelta(hours=2)).isoformat(),))
    _sql(client.db_path, "INSERT INTO runs (id, stage, status, started_at) VALUES ('new', 'profile', 'running', ?)",
         ((now - timedelta(minutes=2)).isoformat(),))
    runs = {r["id"]: r["status"] for r in client.get("/api/runs").json()}
    assert runs == {"old": "stalled", "new": "running"}
