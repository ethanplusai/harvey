"""Tests for tech detection, company signals storage, and prospect export."""

import os
import tempfile

import pytest
import pytest_asyncio

from sellbird.state import StateManager
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.integrations.tech_detect import detect_tech
from sellbird.export import collect_prospects, export_prospects_csv, to_csv, CSV_COLUMNS


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


# ── tech detection ──


def test_detect_tech_matches_known_fingerprints():
    html = """
    <html><head>
      <script src="https://js.hs-scripts.com/12345.js"></script>
      <script src="https://widget.intercom.io/widget/abc"></script>
      <link href="https://cdn.shopify.com/s/files/theme.css">
      <script src="https://www.googletagmanager.com/gtag/js?id=G-1"></script>
    </head><body>wp-content/themes/foo</body></html>
    """
    found = detect_tech(html)
    assert "HubSpot" in found
    assert "Intercom" in found
    assert "Shopify" in found
    assert "Google Analytics" in found
    assert "WordPress" in found
    assert found == sorted(found)


def test_detect_tech_no_false_positives_on_plain_page():
    html = "<html><body><h1>Welcome to Acme Plumbing</h1><p>Call us.</p></body></html>"
    assert detect_tech(html) == []
    assert detect_tech("") == []


# ── company signals storage ──


@pytest.mark.asyncio
async def test_company_signals_roundtrip_and_merge(state):
    cid = await state.add_company(Company(
        name="Acme", domain="acme.com",
        tech_stack=["Shopify"], signals=[{"type": "hiring", "detail": "Head of Growth"}],
    ))
    fetched = await state.get_company(cid)
    assert fetched.tech_stack == ["Shopify"]
    assert fetched.signals[0]["detail"] == "Head of Growth"

    # Merge: new tech appends, duplicate signal is ignored
    await state.update_company_signals(
        cid,
        tech_stack=["Shopify", "Intercom"],
        new_signals=[
            {"type": "hiring", "detail": "Head of Growth"},        # dup
            {"type": "hiring", "detail": "Growth Marketer"},       # new
        ],
    )
    fetched = await state.get_company(cid)
    assert fetched.tech_stack == ["Shopify", "Intercom"]
    assert len(fetched.signals) == 2


@pytest.mark.asyncio
async def test_legacy_company_rows_parse(state):
    """Rows written before v5 (empty JSON columns) must still load."""
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute(
            "INSERT INTO companies (id, name, domain, tech_stack_json, signals_json) "
            "VALUES ('c1', 'Old Co', 'old.com', '', 'not-json')"
        )
        await db.commit()
    company = await state.get_company("c1")
    assert company.tech_stack == []
    assert company.signals == []


# ── export ──


async def _seed_prospects(state):
    specs = [
        ("Ada", "verified", 85, "new"),
        ("Bob", "risky", 70, "new"),
        ("Cal", "guess", 90, "new"),
        ("Dee", "verified", 40, "contacted"),
        ("Eve", "", 0, "new"),  # no email
    ]
    for name, estatus, score, status in specs:
        await state.add_prospect(Prospect(
            first_name=name, last_name="Test", title="VP Marketing",
            email=(f"{name.lower()}@acme.com" if estatus else ""),
            email_status=estatus, email_verified=(estatus == "verified"),
            score=score, status=status, company="Acme",
            personalization_notes="Signal: hiring Growth Lead",
        ))


@pytest.mark.asyncio
async def test_collect_prospects_default_deliverable_only(state):
    await _seed_prospects(state)
    rows = await collect_prospects(state)
    names = {r["first_name"] for r in rows}
    # verified + risky only; guess/no-email excluded
    assert names == {"Ada", "Bob", "Dee"}
    # sorted by score desc
    assert rows[0]["first_name"] == "Ada"


@pytest.mark.asyncio
async def test_collect_prospects_filters(state):
    await _seed_prospects(state)
    rows = await collect_prospects(state, min_score=60, statuses=["new"])
    assert {r["first_name"] for r in rows} == {"Ada", "Bob"}

    rows = await collect_prospects(state, email_statuses=["verified"])
    assert {r["first_name"] for r in rows} == {"Ada", "Dee"}

    rows = await collect_prospects(state, include_all=True)
    assert len(rows) == 5


@pytest.mark.asyncio
async def test_export_csv_shape(state, tmp_path):
    await _seed_prospects(state)
    out = str(tmp_path / "prospects.csv")
    count, text = await export_prospects_csv(state, out_path=out)
    assert count == 3
    assert os.path.exists(out)

    lines = text.strip().splitlines()
    header = lines[0].split(",")
    assert header == CSV_COLUMNS
    assert any("ada@acme.com" in line for line in lines[1:])
    # Personalization/signal text survives export
    assert any("Signal: hiring Growth Lead" in line for line in lines[1:])


def test_to_csv_ignores_extra_keys():
    text = to_csv([{**{c: "" for c in CSV_COLUMNS}, "email": "a@b.com", "junk": "x"}])
    assert "junk" not in text.splitlines()[0]
    assert "a@b.com" in text
