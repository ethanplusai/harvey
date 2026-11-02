"""The Companies tab: fit and status on /api/companies, one company's
profile card, and the company register download."""

import asyncio
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    monkeypatch.setattr(harvey_config, "load_config", lambda *a, **k: CONFIG)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")

    async def _init():
        state = StateManager(db_path=str(db))
        await state.init_db()
        await seed_signal_catalog(state)
    asyncio.run(_init())
    with sqlite3.connect(db) as c:
        c.executemany(
            "INSERT INTO companies (id, name, domain, website, industry, location, source, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, 'discover:test', ?)",
            [("c1", "Acme Forge", "acme.example", "https://acme.example", "forging company",
              "Springfield", "https://search.example/r1"),
             ("c2", "Quiet Mill", "quiet.example", "", "magazine", "Springfield", ""),
             ("c3", "Blocked Co", "blocked.example", "", "forging company", "Springfield", "")])
        c.execute("INSERT INTO prospects (id, company_id, first_name, last_name, title, email, "
                  "email_status, status) VALUES ('p1', 'c1', 'Nora', 'Vale', 'Managing Director', "
                  "'nora@acme.example', 'verified', 'new')")
        c.execute("INSERT INTO observations (id, company_id, prospect_id, signal_code, value_text, "
                  "evidence_url) VALUES ('o1', 'c1', '', 'HIRING_ROLE', 'Forklift Operator', "
                  "'https://acme.example/careers')")
    return {"token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def test_companies_carry_fit_and_status_best_fit_first(client):
    rows = client.get("/api/companies").json()
    assert [r["id"] for r in rows] == ["c1", "c2", "c3"]
    acme = rows[0]
    assert acme["fit"] == 100
    assert "hiring: forklift operator" in acme["fit_reasons"]
    assert acme["register_status"] == "Ready"
    assert rows[2]["register_status"] == "Excluded" and rows[2]["fit"] == 0


def test_profile_has_register_row_and_observations(client):
    data = client.get("/api/companies/c1/profile").json()
    assert data["company"]["domain"] == "acme.example"
    assert data["register"]["status"] == "Ready"
    assert data["register"]["why_selected"].startswith("Found via discover:test; sector: forging company")
    assert [o["signal_code"] for o in data["observations"]] == ["HIRING_ROLE"]
    assert client.get("/api/companies/nope/profile").status_code == 404


def test_register_csv_lists_every_company(client):
    resp = client.get("/api/export/companies.csv")
    assert resp.status_code == 200
    assert "company-register.csv" in resp.headers["content-disposition"]
    lines = resp.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("company,website,sector,area,fit,why_selected,status")
    assert len(lines) == 4
    assert "Quiet Mill" in resp.text and "Not processed yet" in resp.text


def test_best_fit_first_across_more_than_the_page_cap(client, monkeypatch):
    monkeypatch.setattr(dash, "COMPANIES_TAB_LIMIT", 2)
    rows = client.get("/api/companies").json()
    assert [r["id"] for r in rows] == ["c1", "c2"]


def test_register_export_failure_is_an_error(client, monkeypatch):
    import harvey.register as register

    async def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(register, "build_register", broken)
    assert client.get("/api/export/companies.csv").status_code == 500
