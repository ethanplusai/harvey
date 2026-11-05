"""The Contacts tab is sorted, filtered, searched and paged in SQL: the page
never holds the whole list. Every contact here is invented."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.models.prospect import Prospect
from harvey.state import StateManager

PEOPLE = [  # first, last, company, email status, status, score, created
    ("Claire", "Duval", "Lumenhexa", "verified", "new", 82, "2026-10-20 09:00:00"),
    ("Hugo", "Girard", "Brindille", "guess", "queued", 55, "2026-10-22 10:00:00"),
    ("Élodie", "Martin", "Corvane", "invalid", "bounced", 30, "2026-10-18 11:00:00"),
    ("Paul", "Lefort", "Atelier Sorel", "risky", "contacted", 67, "2026-10-25 08:00:00"),
    ("Dana", "Fox", "Northvale", "verified", "replied", 91, "2026-10-15 12:00:00"),
    ("Nina", "Roux", "Quillnest", "", "new", 0, "2026-10-26 07:00:00"),
    ("Yanis", "Benali", "", "verified", "meeting", 74, "2026-10-12 15:00:00"),
]


async def _seed(db_path):
    state = StateManager(str(db_path))
    await state.init_db()
    for first, last, company, email_status, _status, _score, _created in PEOPLE:
        await state.add_prospect(Prospect(
            first_name=first, last_name=last, title="CEO", company=company,
            email=f"{first.lower()}@{(company or 'solo').lower().replace(' ', '')}.example",
            email_status=email_status))
    import sqlite3
    db = sqlite3.connect(db_path)
    for first, last, _company, _email_status, status, score, created in PEOPLE:
        db.execute("UPDATE prospects SET status = ?, score = ?, created_at = ? WHERE first_name = ?",
                   (status, score, created, first))
    db.commit()
    db.close()


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        asyncio.run(_seed(db))
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            yield c


def names(client, **params):
    page = client.get("/api/contacts", params=params).json()
    return [p["first_name"] for p in page["items"]]


def test_newest_first_by_default(client):
    assert names(client) == ["Nina", "Paul", "Hugo", "Claire", "Élodie", "Dana", "Yanis"]


def test_sort_by_each_column(client):
    assert names(client, sort="score", dir="desc")[:3] == ["Dana", "Claire", "Yanis"]
    # Accents don't push Élodie after Yanis.
    assert names(client, sort="name", dir="asc") == ["Claire", "Dana", "Élodie", "Hugo", "Nina", "Paul", "Yanis"]
    # No company sorts last, whichever the direction.
    assert names(client, sort="company", dir="asc")[-1] == "Yanis"
    assert names(client, sort="company", dir="desc")[-1] == "Yanis"
    assert names(client, sort="email", dir="asc")[:2] == ["Claire", "Dana"]
    # Status follows the pipeline: new, queued, contacted, replied, meeting, then the rest.
    assert names(client, sort="status", dir="asc") == ["Nina", "Claire", "Hugo", "Paul", "Dana", "Yanis", "Élodie"]
    assert names(client, sort="added", dir="asc")[0] == "Yanis"


def test_unknown_sort_falls_back_to_newest(client):
    assert names(client, sort="password; DROP TABLE prospects") == names(client)


def test_filter_by_email_status(client):
    assert names(client, email="verified", sort="name", dir="asc") == ["Claire", "Dana", "Yanis"]
    assert names(client, email="invalid") == ["Élodie"]
    assert names(client, email="risky") == ["Paul"]
    # An address with no recorded verdict is unverified.
    assert set(names(client, email="guess")) == {"Hugo", "Nina"}


def test_counts_honour_the_other_filter_and_the_search(client):
    page = client.get("/api/contacts", params={"status": "todo"}).json()
    assert page["counts"]["email"] == {"verified": 1, "risky": 0, "guess": 2, "invalid": 0, "all": 3}
    assert page["counts"]["status"]["all"] == 7 and page["everyone"] == 7
    page = client.get("/api/contacts", params={"email": "verified"}).json()
    assert page["counts"]["status"] == {"todo": 1, "contacted": 0, "replied": 2, "closed": 0, "all": 3}


def test_search_ignores_case_and_accents_and_wildcards(client):
    assert names(client, q="elodie") == ["Élodie"]
    assert names(client, q="NORTHVALE") == ["Dana"]
    assert names(client, q="%") == []


def test_pages(client):
    page = client.get("/api/contacts", params={"limit": 3, "offset": 3}).json()
    assert [p["first_name"] for p in page["items"]] == ["Claire", "Élodie", "Dana"]
    assert page["total"] == 7 and page["offset"] == 3 and page["limit"] == 3
    assert client.get("/api/contacts", params={"limit": 10000}).json()["limit"] == 200
