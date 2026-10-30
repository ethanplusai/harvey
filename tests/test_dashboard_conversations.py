"""/api/conversations carries the whole exchange: the cold emails a reply
answers live in the Outbox, not in the stored thread."""

import asyncio
import json
import sqlite3

from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from harvey.state import StateManager


def _sql(db, query, params=()):
    with sqlite3.connect(db) as conn:
        conn.execute(query, params)
    conn.close()


def _seed(db):
    asyncio.run(StateManager(str(db)).init_db())
    _sql(db, "INSERT INTO prospects (id, first_name, email, status) VALUES ('p1', 'Iris', 'i@x.test', 'replied')")
    _sql(db, "INSERT INTO prospects (id, first_name, email, status) VALUES ('p2', 'Ben', 'b@x.test', 'contacted')")
    thread = [{"sender": "prospect", "content": "What does it cost?", "timestamp": "2026-10-18T10:00:00"}]
    _sql(db, "INSERT INTO conversations (id, prospect_id, campaign_id, thread_json, intent, status, updated_at) "
             "VALUES ('c1', 'p1', 'k1', ?, 'question', 'open', '2026-10-18T10:00:00')", (json.dumps(thread),))
    rows = [
        ("o1", "p1", "sequence", 1, "sent", "quick question", "Hi Iris, one.", "2026-10-10T09:00:00", ""),
        ("o2", "p1", "sequence", 2, "sent", "re: booking", "Hi Iris, two.", "2026-10-13T09:00:00", ""),
        ("o3", "p1", "sequence", 3, "cancelled", "closing", "Hi Iris, three.", None, ""),
        ("o4", "p1", "reply", 1, "pending_review", "Re: re: booking", "It's $49 a month.", None, "c1"),
        ("o5", "p1", "reply", 1, "sent", "Re: earlier", "Already in the thread.", "2026-10-18T11:00:00", "c1"),
        ("o6", "p2", "sequence", 1, "sent", "hello Ben", "Hi Ben.", "2026-10-11T09:00:00", ""),
    ]
    for oid, pid, kind, step, status, subject, body, sent_at, conv in rows:
        _sql(db, "INSERT INTO outbox (id, prospect_id, campaign_id, conversation_id, kind, step, to_email, "
                 "subject, body, status, sent_at, send_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
             (oid, pid, "k1" if kind == "sequence" else "", conv, kind, step, "x@x.test", subject, body,
              status, sent_at, "2026-10-19T09:00:00"))


def test_conversations_include_the_emails_they_answer_and_a_waiting_draft(client):
    _seed(client.db_path)
    [convo] = client.get("/api/conversations").json()
    assert [(e["step"], e["subject"]) for e in convo["emails"]] == [(1, "quick question"), (2, "re: booking")]
    assert all("sent_at" in e and "body" in e for e in convo["emails"])   # cancelled step 3 left out
    assert convo["draft"]["id"] == "o4" and convo["draft"]["body"] == "It's $49 a month."
    assert convo["thread"][0]["content"] == "What does it cost?"


def test_a_conversation_without_emails_or_draft(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    _sql(client.db_path, "INSERT INTO prospects (id, first_name, email, status) VALUES ('p9', 'Ana', 'a@x.test', 'replied')")
    _sql(client.db_path, "INSERT INTO conversations (id, prospect_id, thread_json, status) VALUES ('c9', 'p9', '[]', 'open')")
    [convo] = client.get("/api/conversations").json()
    assert convo["emails"] == [] and convo["draft"] is None


def test_a_prospect_in_two_campaigns_sees_only_the_sequence_the_reply_answers(client):
    _seed(client.db_path)
    _sql(client.db_path, "INSERT INTO outbox (id, prospect_id, campaign_id, kind, step, to_email, subject, body, "
         "status, sent_at, send_at) VALUES ('o7', 'p1', 'k2', 'sequence', 1, 'x@x.test', 'other campaign', "
         "'Hi again.', 'sent', '2026-09-01T09:00:00', '2026-09-01T09:00:00')")
    [convo] = client.get("/api/conversations").json()
    assert [e["subject"] for e in convo["emails"]] == ["quick question", "re: booking"]
    assert all("campaign_id" not in e for e in convo["emails"])


def test_an_escalated_conversation_shows_on_today_and_can_be_handed_back(client):
    db = client.db_path
    asyncio.run(StateManager(str(db)).init_db())
    _sql(db, "INSERT INTO prospects (id, first_name, email, status) VALUES ('p1', 'Iris', 'i@x.test', 'replied')")
    _sql(db, "INSERT INTO conversations (id, prospect_id, status) VALUES ('c1', 'p1', 'needs_human')")
    today = client.get("/api/today").json()
    assert today["stats"]["needs_human"] == 1
    assert any(i["key"] == "needs-human" and i["tab"] == "conversations" for i in today["items"])

    assert client.post("/api/conversations/c1/status", json={"status": "open"}).json()["success"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM conversations WHERE id='c1'").fetchone()[0] == "open"
        logged = conn.execute("SELECT details_json FROM actions WHERE action_type='conversation_status'").fetchone()
    conn.close()
    assert json.loads(logged[0]) == {"conversation_id": "c1", "from": "needs_human", "to": "open"}
    assert not any(i["key"] == "needs-human" for i in client.get("/api/today").json()["items"])


def test_conversation_status_rejects_bad_input(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    _sql(client.db_path, "INSERT INTO conversations (id, status) VALUES ('c1', 'open')")
    assert client.post("/api/conversations/c1/status", json={"status": "needs_human"}).status_code == 400
    assert client.post("/api/conversations/c1/status", json=["closed"]).status_code == 400
    assert client.post("/api/conversations/nope/status", json={"status": "closed"}).status_code == 404


def _render_actions(convo):
    """Run app.js's convoActions under node with the real escaping helpers."""
    import re
    import shutil
    import subprocess
    from pathlib import Path
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = (Path(__file__).resolve().parent.parent / "harvey" / "web" / "app.js").read_text(encoding="utf-8")
    parts = []
    for pattern in (r"^function escHtml\(.*?^}\n", r"^function jsq\(.*?^}\n",
                    r"^function icon\(.*?^}\n", r"^function convoActions\(.*?^}\n"):
        match = re.search(pattern, source, re.S | re.M)
        assert match, pattern
        parts.append(match.group(0))
    script = "\n".join(parts) + f"\nprocess.stdout.write(convoActions({json.dumps(convo)}));"
    return subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8",
                          check=True, timeout=30).stdout


def test_an_escalation_from_someone_who_opted_out_cant_be_handed_back():
    html = _render_actions({"id": "c1", "status": "needs_human", "prospect_status": "opted_out",
                            "prospect_id": "p1", "prospect_email": "i@x.test"})
    assert "Opted out, and Harvey handed this to you" in html
    assert "Hand back to Harvey" not in html
    assert "Close</button>" in html and "Why opted out?" in html
    # Anyone else's escalation can still go back to Harvey.
    html = _render_actions({"id": "c2", "status": "needs_human", "prospect_status": "replied",
                            "prospect_id": "p2", "prospect_email": "b@x.test"})
    assert "Hand back to Harvey" in html and "Why opted out?" not in html
