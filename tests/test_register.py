"""The company register: every company, why it fits, why it wasn't contacted."""

from types import SimpleNamespace

import pytest

from harvey.register import build_register, company_status, to_csv

CO = {"id": "c1", "name": "Acme Forge", "domain": "acme.example", "industry": "forging company",
      "location": "Springfield", "website": "https://acme.example", "created_at": "2026-01-02 10:00",
      "source": "osm"}


def p(email_status="verified", status="new", first="Nora", title="Managing Director"):
    return {"company_id": "c1", "first_name": first, "last_name": "Vale", "title": title,
            "email": "nora@acme.example", "email_status": email_status, "status": status}


@pytest.mark.parametrize("prospects,outbox,expect", [
    ([p()], [{"status": "sent"}], "Emailed"),
    ([p(status="replied")], [], "Replied"),
    ([p()], [{"status": "approved", "send_at": "2026-01-05T04:00"}], "Approved"),
    ([p()], [{"status": "pending_review"}], "Draft waiting"),
    ([p()], [], "Ready"),
    ([p("risky")], [], "Not contacted"),
    ([p("invalid")], [], "Not contacted"),
    ([p(status="lost")], [], "Not contacted"),
])
def test_status_follows_what_harvey_did(prospects, outbox, expect):
    assert company_status(CO, prospects, outbox, [], [])[0] == expect


def test_reasons_say_why_nothing_went_out():
    assert "risky" in company_status(CO, [p("risky")], [], [], [])[1]
    assert "no valid email" in company_status(CO, [p("guess")], [], [], [])[1]
    form = [{"signal_code": "CONTACT_FORM_URL", "value_text": "https://acme.example/contact"}]
    why = company_status({**CO, "prospects_checked_at": "x"}, [], [], form, [])[1]
    assert "no named person" in why and "Contact form available" in why
    assert company_status(CO, [], [], [], [])[0] == "Not processed yet"


def test_risky_is_ready_when_you_send_to_risky():
    assert company_status(CO, [p("risky")], [], [], [], send_to_risky=True)[0] == "Ready"
    assert company_status(CO, [p("risky")], [], [], [])[0] == "Not contacted"


def test_an_opt_out_is_not_shown_as_emailed():
    status, why = company_status(CO, [p(status="opted_out")], [{"status": "sent"}], [], [])
    assert status == "Opted out" and "asked not to be emailed" in why


def test_do_not_contact_says_only_what_it_knows():
    assert company_status(CO, [], [], [], ["acme.example"])[1] == "On your do-not-contact list"


@pytest.mark.asyncio
async def test_register_reads_the_live_do_not_contact_list(monkeypatch):
    import harvey.register as register
    monkeypatch.setattr(register, "live_do_not_contact", lambda config: ["acme.example"])
    from harvey.config import ComplianceConfig
    config = SimpleNamespace(
        icp=SimpleNamespace(industries=[], titles=[], hiring_signals=[]),
        compliance=ComplianceConfig())

    async def query_db(sql, params=()):
        return [CO] if "FROM companies" in sql else []

    row = (await build_register(query_db, config))[0]
    assert row["status"] == "Excluded" and row["fit"] == 0


def test_do_not_contact_is_excluded_first():
    assert company_status(CO, [p()], [{"status": "sent"}], [], ["acme.example"])[0] == "Excluded"


@pytest.mark.asyncio
async def test_register_keeps_companies_without_contacts_and_is_spreadsheet_safe():
    config = SimpleNamespace(
        icp=SimpleNamespace(industries=["forging company"], titles=["Managing Director"], hiring_signals=[]),
        compliance=SimpleNamespace(do_not_contact=[]))
    other = {**CO, "id": "c2", "name": "=HYPERLINK(\"x\")", "domain": "beta.example", "website": ""}

    async def query_db(sql, params=()):
        if "FROM companies" in sql:
            return [CO, other]
        if "FROM prospects" in sql:
            return [p()]
        return []

    rows = await build_register(query_db, config)
    assert [r["company"] for r in rows] == ["Acme Forge", "=HYPERLINK(\"x\")"]  # best fit first
    assert rows[0]["why_selected"].startswith("Found via osm; sector: forging company")
    assert rows[1]["status"] == "Not processed yet" and rows[1]["website"] == "https://beta.example"
    assert "'=HYPERLINK" in to_csv(rows)


def test_cli_writes_the_register(tmp_path, monkeypatch, capsys):
    import asyncio
    import sqlite3

    import harvey.config as harvey_config
    import harvey.state as harvey_state
    from harvey.cli import cmd_companies
    from harvey.state import StateManager

    db = tmp_path / "harvey.db"
    asyncio.run(StateManager(db_path=str(db)).init_db())
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO companies (id, name, domain, industry, location) "
                  "VALUES ('c1', 'Acme Forge', 'acme.example', 'forging company', 'Springfield')")
    monkeypatch.setattr(harvey_state, "DB_PATH", db)
    monkeypatch.setattr(harvey_config, "load_config", lambda *a, **k: SimpleNamespace(
        icp=SimpleNamespace(industries=["forging company"], titles=[], hiring_signals=[]),
        compliance=SimpleNamespace(do_not_contact=[])))
    out = tmp_path / "register.csv"
    cmd_companies(SimpleNamespace(out=str(out)))
    text = out.read_text(encoding="utf-8-sig")
    assert "Acme Forge" in text and text.startswith("company,website")
    assert "1 companies" in capsys.readouterr().out


def test_cli_without_a_database_says_so(tmp_path, monkeypatch, capsys):
    import harvey.state as harvey_state
    from harvey.cli import cmd_companies

    monkeypatch.setattr(harvey_state, "DB_PATH", tmp_path / "missing.db")
    cmd_companies(SimpleNamespace(out=str(tmp_path / "x.csv")))
    assert "No database yet" in capsys.readouterr().out
    assert not (tmp_path / "x.csv").exists()
