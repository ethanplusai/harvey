"""Daily dashboard counters use the same local date as usage_log writers."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest

import sellbird.dashboard as dash
from sellbird.state import StateManager
from tests.test_dashboard_signals import client  # noqa: F401


@pytest.mark.parametrize("offset", [-1, 1])
def test_stats_use_local_day_when_sqlite_utc_day_differs(client, monkeypatch, offset):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    with closing(sqlite3.connect(client.db_path)) as db, db:
        utc_day = date.fromisoformat(db.execute("SELECT date('now')").fetchone()[0])
        local_day = utc_day + timedelta(days=offset)
        db.executemany(
            "INSERT INTO usage_log (id, date, claude_calls, codex_calls) VALUES (?, ?, ?, ?)",
            [("utc", utc_day.isoformat(), 91, 92),
             ("local", local_day.isoformat(), 3, 7)],
        )

    class LocalDate(date):
        @classmethod
        def today(cls):
            return local_day

    monkeypatch.setattr(dash, "date", LocalDate)
    response = client.get("/api/stats")
    assert response.status_code == 200
    stats = response.json()
    assert stats["claude_calls_today"] == 3
    assert stats["codex_calls_today"] == 7
