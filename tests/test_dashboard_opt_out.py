"""Why someone is opted out, and undoing it from the dashboard.

An opt-out is permanent to Harvey (state.update_prospect_status never moves
anyone off it), so a mistaken one needs a deliberate human action, and the
human needs to see what triggered it before deciding."""

import asyncio
import json
import sqlite3

from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from harvey.state import StateManager


def _sql(db, query, params=()):
    with sqlite3.connect(db) as conn:
        rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def _seed(db, status="opted_out"):
    asyncio.run(StateManager(str(db)).init_db())
    _sql(db, "INSERT INTO prospects (id, first_name, email, email_status, status) "
             "VALUES ('p1', 'Iris', 'i@x.test', 'verified', ?)", (status,))


def _record_opt_out(db, **details):
    d = {"prospect_id": "p1", "prospect_email": "i@x.test", "rule": "only_words",
         "matched": "stop", "why": 'Their reply was only "stop".', "message": "STOP",
         "conversation_id": "c1", **details}
    _sql(db, "INSERT INTO actions (id, action_type, agent, details_json, created_at) "
             "VALUES ('a1', 'opt_out', 'handler', ?, '2026-10-20T09:00:00')", (json.dumps(d),))


def test_shows_what_triggered_the_opt_out(client):
    _seed(client.db_path)
    _record_opt_out(client.db_path)
    data = client.get("/api/prospects/p1/opt-out").json()
    assert data["opted_out"] is True and data["recorded"] is True
    assert (data["rule"], data["matched"], data["message"]) == ("only_words", "stop", "STOP")
    assert data["why"] == 'Their reply was only "stop".'
    assert data["at"].startswith("2026-10-20")
    assert [h["action"] for h in data["history"]] == ["opt_out"]


def test_an_opt_out_from_before_reasons_were_kept_shows_their_last_message(client):
    _seed(client.db_path)
    thread = [{"sender": "harvey", "content": "Hi Iris"},
              {"sender": "prospect", "content": "please stop", "timestamp": "2026-10-19T08:00:00"}]
    _sql(client.db_path, "INSERT INTO conversations (id, prospect_id, thread_json, status, updated_at) "
                         "VALUES ('c1', 'p1', ?, 'closed', '2026-10-19T08:00:00')", (json.dumps(thread),))
    data = client.get("/api/prospects/p1/opt-out").json()
    assert data["opted_out"] is True and data["recorded"] is False
    assert data["message"] == "please stop" and data["conversation_id"] == "c1"
    assert data["rule"] is None


def test_someone_who_never_opted_out(client):
    _seed(client.db_path, status="contacted")
    data = client.get("/api/prospects/p1/opt-out").json()
    assert data["opted_out"] is False and data["status"] == "contacted"
    assert client.get("/api/prospects/nobody/opt-out").status_code == 404


def test_undo_needs_an_explicit_confirm(client):
    _seed(client.db_path)
    for body in (None, {}, {"confirm": False}, {"confirm": "yes"}, [1]):
        r = client.post("/api/prospects/p1/opt-out/undo", json=body)
        assert r.status_code == 400
    assert _sql(client.db_path, "SELECT status FROM prospects")[0][0] == "opted_out"


def test_undo_puts_them_back_as_replied_and_keeps_the_record(client):
    _seed(client.db_path)
    _record_opt_out(client.db_path)
    _sql(client.db_path, "INSERT INTO outbox (id, prospect_id, kind, step, to_email, subject, body, "
                         "status, send_at) VALUES ('o1', 'p1', 'sequence', 2, 'i@x.test', 's', 'b', "
                         "'cancelled', '2026-10-19T09:00:00')")
    r = client.post("/api/prospects/p1/opt-out/undo", json={"confirm": True})
    assert r.status_code == 200 and r.json() == {"success": True, "status": "replied"}

    # 'replied' never restarts the sequence: what the opt-out cancelled stays cancelled.
    assert _sql(client.db_path, "SELECT status FROM prospects")[0][0] == "replied"
    assert _sql(client.db_path, "SELECT status FROM outbox")[0][0] == "cancelled"

    data = client.get("/api/prospects/p1/opt-out").json()
    assert data["opted_out"] is False
    assert [h["action"] for h in data["history"]] == ["opt_out", "opt_out_undone"]
    [undone] = [json.loads(d) for (d,) in _sql(
        client.db_path, "SELECT details_json FROM actions WHERE action_type = 'opt_out_undone'")]
    assert undone["prospect_id"] == "p1" and undone["previous_why"] == 'Their reply was only "stop".'


def test_undo_refuses_someone_who_is_not_opted_out(client):
    _seed(client.db_path, status="replied")
    r = client.post("/api/prospects/p1/opt-out/undo", json={"confirm": True})
    assert r.status_code == 409
    assert client.post("/api/prospects/nobody/opt-out/undo", json={"confirm": True}).status_code == 404


def _render_panel(prospect, data, armed=False):
    """Run app.js's optOutPanel under node with the real escaping helpers."""
    import re
    import shutil
    import subprocess
    from pathlib import Path
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    web = Path(__file__).resolve().parent.parent / "harvey" / "web"
    source = (web / "app.js").read_text(encoding="utf-8")
    parts = [(web / "i18n.js").read_text(encoding="utf-8")]  # t(), loaded before app.js
    for pattern in (r"^function escHtml\(.*?^}\n", r"^function own\(.*?^}\n",
                    r"^const OPT_OUT_RULES = \{.*?^\};\n", r"^function optOutPanel\(.*?^}\n"):
        match = re.search(pattern, source, re.S | re.M)
        assert match, pattern
        parts.append(match.group(0))
    script = ("const relTime = d => 'WHEN';\nconst statusMeta = s => ({label: s.charAt(0).toUpperCase() + s.slice(1)});\n" + "\n".join(parts) +
              f"\nlet _optOut = {{id: 'p1', data: {json.dumps(data)}, armed: {json.dumps(armed)}, busy: false}};"
              f"\nprocess.stdout.write(optOutPanel({json.dumps(prospect)}));")
    return subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8",
                          check=True, timeout=30).stdout


def test_the_panel_shows_the_reason_and_escapes_their_message():
    html = _render_panel(
        {"first_name": "Iris", "last_name": "<b>X</b>", "email": "i@x.test"},
        {"opted_out": True, "recorded": True, "rule": "only_words", "why": 'Their reply was only "stop".',
         "message": "<img src=x onerror=alert(1)>STOP", "at": "2026-10-20", "history": []})
    assert "Their reply was only &quot;stop&quot;." in html
    assert "Their reply was only an opt-out word" in html
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;STOP" in html
    assert "<b>X</b>" not in html
    assert "Undo opt-out" in html and "Confirm undo" not in html


def test_the_panel_asks_before_undoing_and_says_what_happens():
    html = _render_panel({"first_name": "Iris", "email": "i@x.test"},
                         {"opted_out": True, "recorded": False, "message": "", "history": []}, armed=True)
    assert "before Harvey kept a record" in html
    assert "Confirm undo" in html and "Nothing is sent automatically" in html


def test_the_panel_after_an_undo():
    html = _render_panel({"first_name": "Iris", "email": "i@x.test"},
                         {"opted_out": False, "recorded": True, "status": "replied", "rule": "phrase",
                          "why": "w", "message": "m",
                          "history": [{"action": "opt_out", "at": "a"}, {"action": "opt_out_undone", "at": "b"}]})
    assert "You undid an opt-out" in html and "Not opted out now (Replied)" in html
    assert "Opt-out history for Iris" in html
    assert "Undo opt-out" not in html


def test_an_undone_opt_out_without_a_reason_still_shows_their_message(client):
    _seed(client.db_path)
    thread = [{"sender": "prospect", "content": "no thanks", "timestamp": "2026-10-19T08:00:00"}]
    _sql(client.db_path, "INSERT INTO conversations (id, prospect_id, thread_json, status, updated_at) "
                         "VALUES ('c1', 'p1', ?, 'closed', '2026-10-19T08:00:00')", (json.dumps(thread),))
    assert client.post("/api/prospects/p1/opt-out/undo", json={"confirm": True}).status_code == 200
    data = client.get("/api/prospects/p1/opt-out").json()
    assert data["opted_out"] is False and data["recorded"] is False
    assert data["message"] == "no thanks"
    assert [h["action"] for h in data["history"]] == ["opt_out_undone"]


def test_the_contact_list_flags_an_undone_opt_out(client):
    _seed(client.db_path)
    _sql(client.db_path, "INSERT INTO prospects (id, first_name, email, status) VALUES ('p2', 'Ben', 'b@x.test', 'new')")
    assert client.post("/api/prospects/p1/opt-out/undo", json={"confirm": True}).status_code == 200
    flags = {p["id"]: p["opt_out_undone"] for p in client.get("/api/prospects").json()}
    assert flags == {"p1": True, "p2": False}
