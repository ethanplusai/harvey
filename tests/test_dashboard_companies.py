"""The Companies tab is sorted and paged in SQL, with each company's number
of contacts. Every business here is invented."""

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager

COMPANIES = [  # name, industry, location, size, source, created, contacts
    ("Lumenhexa", "SaaS", "Toulouse, France", "11-50 employees", "discover:serper", "2026-10-20 09:00:00", 2),
    ("Brindille", "healthtech", "Lyon, France", "1-10", "web_search", "2026-10-22 10:00:00", 0),
    ("Éclat Studio", "", "Paris, France", "", "discover:serper", "2026-10-18 11:00:00", 1),
    ("Northvale", "proptech", "", "200+", "discover:osm", "2026-10-25 08:00:00", 3),
]


async def _seed(db_path):
    state = StateManager(str(db_path))
    await state.init_db()
    for name, industry, location, size, source, _created, contacts in COMPANIES:
        domain = name.lower().replace(" ", "").replace("é", "e") + ".example"
        cid = await state.add_company(Company(name=name, domain=domain, industry=industry,
                                              location=location, company_size=size, source=source))
        for n in range(contacts):
            await state.add_prospect(Prospect(first_name=f"P{n}", last_name="Test", title="CEO",
                                              email=f"p{n}@{domain}", company=name, company_id=cid))
    db = sqlite3.connect(db_path)
    for name, *_rest in COMPANIES:
        db.execute("UPDATE companies SET created_at = ? WHERE name = ?", (_rest[4], name))
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
    return [c["name"] for c in client.get("/api/companies", params=params).json()["items"]]


def test_newest_first_with_contact_counts(client):
    page = client.get("/api/companies", params={"sort": "added"}).json()
    assert [(c["name"], c["contact_count"]) for c in page["items"]] == [
        ("Northvale", 3), ("Brindille", 0), ("Lumenhexa", 2), ("Éclat Studio", 1)]
    assert page["total"] == 4


def test_sort_by_each_column(client):
    # Accents don't push Éclat after Northvale.
    assert names(client, sort="name", dir="asc") == ["Brindille", "Éclat Studio", "Lumenhexa", "Northvale"]
    assert names(client, sort="contacts", dir="desc") == ["Northvale", "Lumenhexa", "Éclat Studio", "Brindille"]
    # Size by its first number; no size sorts last either way.
    assert names(client, sort="size", dir="asc") == ["Brindille", "Lumenhexa", "Northvale", "Éclat Studio"]
    assert names(client, sort="size", dir="desc")[-1] == "Éclat Studio"
    assert names(client, sort="industry", dir="asc") == ["Brindille", "Northvale", "Lumenhexa", "Éclat Studio"]
    assert names(client, sort="location", dir="desc")[-1] == "Northvale"
    assert names(client, sort="source", dir="asc")[0] == "Northvale"           # discover:osm
    assert names(client, sort="added", dir="asc")[0] == "Éclat Studio"


def test_unknown_sort_falls_back_to_newest(client):
    assert names(client, sort="name; DROP TABLE companies") == names(client, sort="added")


def test_pages(client):
    page = client.get("/api/companies", params={"sort": "added", "limit": 2, "offset": 2}).json()
    assert [c["name"] for c in page["items"]] == ["Lumenhexa", "Éclat Studio"]
    assert (page["total"], page["offset"], page["limit"]) == (4, 2, 2)
    assert client.get("/api/companies", params={"limit": 999}).json()["limit"] == 200
