"""/api/trend: daily counts for the dashboard's sparklines."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from sellbird.state import StateManager


def _sql(db, query, params=()):
    with sqlite3.connect(db) as conn:
        conn.execute(query, params)
    conn.close()


def test_trend_counts_each_day_and_fills_gaps_with_zero(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    today = datetime.now(timezone.utc).date()  # stored times are UTC
    d = lambda n: (today - timedelta(days=n)).isoformat() + "T10:00:00"  # noqa: E731
    for n, day in enumerate((0, 0, 2)):
        _sql(client.db_path, "INSERT INTO prospects (id, first_name, status, created_at) "
             "VALUES (?, 'A', 'new', ?)", (f"p{n}", d(day)))
    _sql(client.db_path, "INSERT INTO outbox (id, prospect_id, to_email, status, sent_at) "
         "VALUES ('o1', 'p0', 'a@x.test', 'sent', ?)", (d(1),))
    _sql(client.db_path, "INSERT INTO outbox (id, prospect_id, to_email, status, sent_at) "
         "VALUES ('o2', 'p0', 'a@x.test', 'pending_review', ?)", (d(1),))
    _sql(client.db_path, "INSERT INTO conversations (id, prospect_id, created_at) "
         "VALUES ('c1', 'p0', ?)", (d(0),))

    data = client.get("/api/trend?days=7").json()
    assert len(data["days"]) == 7 and data["days"][-1] == today.isoformat()
    assert data["found"][-1] == 2 and data["found"][-3] == 1 and sum(data["found"]) == 3
    assert data["sent"][-2] == 1 and sum(data["sent"]) == 1       # only sent mail counts
    assert data["replies"][-1] == 1


def test_trend_days_are_bounded(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    assert len(client.get("/api/trend?days=500").json()["days"]) == 90
    assert len(client.get("/api/trend?days=0").json()["days"]) == 1
    assert client.get("/api/trend?days=x").status_code == 422


def test_trend_counts_days_in_the_viewers_time_zone(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    now = datetime.now(timezone.utc)
    # 3 hours ago UTC is "today" in UTC but, 10 hours west, belongs to whatever
    # local day it was then; compute the expected local day the same way.
    stamp = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    _sql(client.db_path, "INSERT INTO prospects (id, first_name, status, created_at) VALUES ('p1', 'A', 'new', ?)", (stamp,))
    local = (now - timedelta(hours=3) - timedelta(minutes=600)).date().isoformat()
    data = client.get("/api/trend?days=7&tz=-600").json()
    assert data["days"][-1] == (now - timedelta(minutes=600)).date().isoformat()
    assert data["found"][data["days"].index(local)] == 1
    assert len(client.get("/api/trend?days=3&tz=99999").json()["days"]) == 3   # clamped, no crash
