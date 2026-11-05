"""The Companies tab's filters (name or domain, location, source, status, a fit
range) and editing a company: its details, and notes kept for the record.
Every business here is invented."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import harvey.config as harvey_config
import harvey.dashboard as dash
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

ORIGIN = "http://127.0.0.1:5555"
CONFIG = SimpleNamespace(
    icp=SimpleNamespace(industries=["forging company"], titles=["Managing Director"],
                        hiring_signals=["Forklift Operator"]),
    compliance=SimpleNamespace(do_not_contact=["blocked.example"]),
)
COMPANIES = [  # id, name, domain, industry, location, source
    ("a1", "Éclat Forge", "eclat.example", "forging company", "Lyon, France", "discover:serper"),
    ("a2", "Northpeak Mill", "northpeak.example", "magazine", "Toulouse, France", "web_search"),
    ("a3", "Blocked Co", "blocked.example", "forging company", "Paris, France", "discover:serper"),
    ("a4", "Lumen Forge", "lumen.example", "forging company", "Toulouse", "discover:osm"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(harvey_config, "load_config", lambda *a, **k: CONFIG)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")

    async def _init():
        state = StateManager(db_path=str(db))
        await state.init_db()
        await seed_signal_catalog(state)
    asyncio.run(_init())
    with sqlite3.connect(db) as c:
        c.executemany(
            "INSERT INTO companies (id, name, domain, industry, location, source) VALUES (?, ?, ?, ?, ?, ?)",
            COMPANIES)
        c.execute("INSERT INTO prospects (id, company_id, first_name, last_name, title, email, company, "
                  "email_status, status) VALUES ('p1', 'a1', 'Nora', 'Vale', 'Managing Director', "
                  "'nora@eclat.example', 'Éclat Forge', 'verified', 'new')")
        c.execute("INSERT INTO observations (id, company_id, prospect_id, signal_code, value_text) "
                  "VALUES ('o1', 'a1', '', 'HIRING_ROLE', 'Forklift Operator')")
    return {"db": db, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def page(client, **params):
    return client.get("/api/companies", params=params).json()


def ids(client, **params):
    return {c["id"] for c in page(client, **params)["items"]}


# ── Filters ──

def test_search_name_ignoring_accents_or_domain(client):
    assert ids(client, q="eclat") == {"a1"}
    assert ids(client, q="ÉCLAT") == {"a1"}
    assert ids(client, q="northpeak.ex") == {"a2"}
    assert page(client, q="nothing like it")["total"] == 0


def test_location_and_source(client):
    assert ids(client, location="toulouse") == {"a2", "a4"}
    assert ids(client, source="discover:serper") == {"a1", "a3"}
    assert ids(client, source="discover:serper", location="paris") == {"a3"}


def test_status_and_fit_range_follow_the_register(client):
    every = page(client)["items"]
    fit = {c["id"]: c["fit"] for c in every}
    status = {c["id"]: c["register_status"] for c in every}
    assert status["a3"] == "Excluded" and fit["a1"] > fit["a3"]
    assert ids(client, status="Excluded") == {k for k, s in status.items() if s == "Excluded"}
    low = fit["a1"]
    assert ids(client, fit_min=low) == {k for k, f in fit.items() if f >= low}
    assert ids(client, fit_max=0) == {k for k, f in fit.items() if f == 0}
    assert ids(client, fit_min=1, location="toulouse") == {
        k for k in ("a2", "a4") if fit[k] >= 1}


def test_sorting_by_fit_keeps_the_filters(client):
    data = page(client, sort="fit", location="toulouse")
    assert {c["id"] for c in data["items"]} == {"a2", "a4"} and data["total"] == 2
    fits = [c["fit"] for c in data["items"]]
    assert fits == sorted(fits, reverse=True)


def test_facets_list_every_source_and_status_with_counts(client):
    facets = page(client, q="eclat")["facets"]          # facets ignore the filters
    assert dict(facets["sources"]) == {"discover:osm": 1, "discover:serper": 2, "web_search": 1}
    assert sum(n for _, n in facets["statuses"]) == 4
    # Tenths of the fit scale, for the slider's histogram; 100 counts in the last.
    fit = {c["id"]: c["fit"] for c in page(client)["items"]}
    assert len(facets["fit"]) == 10 and sum(facets["fit"]) == 4
    assert facets["fit"][min(fit["a1"] // 10, 9)] >= 1


# ── Editing ──

def test_edit_details_and_notes_and_log_what_changed(client, env):
    resp = client.post("/api/companies/a2", json={
        "notes": "Called 5 Nov: not before Q1.\nAsk again in January.", "location": "Toulouse",
        "domain": "elsewhere.example"})            # not editable: ignored
    assert resp.json() == {"success": True, "changed": ["location", "notes"]}
    company = client.get("/api/companies/a2/profile").json()["company"]
    assert company["notes"].startswith("Called 5 Nov") and company["domain"] == "northpeak.example"
    assert company["hand_edited"] == 0             # notes alone don't lock anything
    with sqlite3.connect(env["db"]) as c:
        details = json.loads(c.execute("SELECT details_json FROM actions WHERE action_type = "
                                       "'company_edited'").fetchone()[0])
    assert details["changes"]["location"] == {"old": "Toulouse, France", "new": "Toulouse"}
    # Saving the same values again changes nothing and logs nothing.
    assert client.post("/api/companies/a2", json={"location": "Toulouse"}).json()["changed"] == []


def test_a_new_name_reaches_prospects_and_survives_automatic_renaming(client, env):
    assert client.post("/api/companies/a1", json={"name": "Eclat Forge SAS"}).json()["success"]
    state = StateManager(db_path=str(env["db"]))
    asyncio.run(state.rename_company("a1", "Some Homepage Title"))
    with sqlite3.connect(env["db"]) as c:
        assert c.execute("SELECT name, hand_edited FROM companies WHERE id = 'a1'").fetchone() == (
            "Eclat Forge SAS", 1)
        assert c.execute("SELECT company FROM prospects WHERE id = 'p1'").fetchone()[0] == "Eclat Forge SAS"


@pytest.mark.parametrize("body, words", [
    ({"name": "  "}, "needs a name"),
    ({"website": "ftp://lumen.example"}, "http"),
    ({"website": "lumen.example"}, "http"),
    ({"notes": "x" * 10001}, "limit"),
    ({"location": 3}, "must be text"),
    ([], "JSON object"),
])
def test_bad_edits_are_refused_with_the_reason(client, body, words):
    resp = client.post("/api/companies/a4", json=body)
    assert resp.status_code == 400 and words in resp.json()["message"]


def test_an_unknown_company_is_a_404(client):
    assert client.post("/api/companies/nope", json={"notes": "hi"}).status_code == 404
