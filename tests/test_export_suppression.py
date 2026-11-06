"""Outreach exports honor opt-outs; raw exports retain them for inspection."""

import asyncio
import csv
import io
import sys

import httpx
import pytest
import pytest_asyncio

from sellbird.export import collect_prospects, export_prospects_csv
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager


async def _seed_state(tmp_path):
    state = StateManager(str(tmp_path / "export.db"))
    await state.init_db()
    for name, email_status, status in (
        ("New", "verified", "new"),
        ("Catchall", "risky", "new"),
        ("Contacted", "verified", "contacted"),
        ("Guess", "guess", "new"),
        ("Optout", "verified", "opted_out"),
        ("OptoutCatchall", "risky", "opted_out"),
    ):
        await state.add_prospect(Prospect(
            first_name=name, last_name="Test", title="Owner", company="Example",
            email=f"{name.lower()}@example.test", email_status=email_status,
            status=status, score=80,
        ))
    return state


@pytest_asyncio.fixture
async def state(tmp_path):
    return await _seed_state(tmp_path)


def _names(text):
    return {row["first_name"] for row in csv.DictReader(io.StringIO(text))}


@pytest.mark.asyncio
async def test_outreach_export_excludes_optouts_without_changing_verification(state):
    count, text = await export_prospects_csv(state)
    assert count == 3
    assert _names(text) == {"New", "Catchall", "Contacted"}
    prospect = await state.get_prospect_by_email("optout@example.test")
    assert (prospect.status, prospect.email_status) == ("opted_out", "verified")


@pytest.mark.asyncio
@pytest.mark.parametrize("filters, expected", [
    ({"statuses": ["opted_out"]}, set()),
    ({"statuses": ["new", "opted_out"]}, {"New", "Catchall"}),
    ({"email_statuses": ["risky"]}, {"Catchall"}),
    ({"email_statuses": []}, {"New", "Catchall", "Contacted", "Guess"}),
])
async def test_outreach_filters_cannot_reinclude_optouts(state, filters, expected):
    rows = await collect_prospects(state, **filters)
    assert {row["first_name"] for row in rows} == expected


@pytest.mark.asyncio
async def test_raw_export_keeps_optouts_and_ignores_outreach_filters(state):
    count, text = await export_prospects_csv(
        state, include_all=True, email_statuses=["invalid"],
        min_score=999, statuses=["missing"],
    )
    rows = list(csv.DictReader(io.StringIO(text)))
    assert count == 6
    assert {row["first_name"] for row in rows} == {
        "New", "Catchall", "Contacted", "Guess", "Optout", "OptoutCatchall",
    }
    opted_out = [row for row in rows if row["status"] == "opted_out"]
    assert {row["email_status"] for row in opted_out} == {"verified", "risky"}


@pytest.mark.asyncio
async def test_export_does_not_drop_legacy_null_pipeline_status(state):
    import aiosqlite

    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET status = NULL WHERE first_name = 'New'")
        await db.commit()
    count, text = await export_prospects_csv(state)
    assert count == 3
    assert _names(text) == {"New", "Catchall", "Contacted"}


@pytest.mark.parametrize("all_flag, expected", [
    ([], {"New", "Catchall", "Contacted"}),
    (["--all"], {"New", "Catchall", "Contacted", "Guess", "Optout", "OptoutCatchall"}),
])
def test_cli_export_respects_suppression_and_raw_mode(tmp_path, monkeypatch, all_flag, expected):
    from sellbird import cli, state as state_module

    state = asyncio.run(_seed_state(tmp_path))
    # Redirect only the CLI's default database selection to real temporary state.
    monkeypatch.setattr(state_module, "StateManager", lambda: state)
    output = tmp_path / "prospects.csv"
    monkeypatch.setattr(sys, "argv", ["sellbird", "export", "--out", str(output), *all_flag])
    cli.main()
    assert _names(output.read_text(encoding="utf-8")) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("query, expected", [
    ("", {"New", "Catchall", "Contacted"}),
    ("?all=true", {"New", "Catchall", "Contacted", "Guess", "Optout", "OptoutCatchall"}),
])
async def test_authenticated_dashboard_export_uses_the_same_policy(state, monkeypatch, query, expected):
    from sellbird import dashboard

    monkeypatch.setattr(dashboard, "DB_PATH", state.db_path)
    # conftest redirects this store into a temporary directory for every test.
    token = dashboard.token_store.get()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dashboard.app),
        base_url="http://127.0.0.1:5555",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        response = await client.get("/api/export/prospects.csv" + query)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert _names(response.text) == expected
