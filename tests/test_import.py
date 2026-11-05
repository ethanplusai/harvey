"""`harvey import`: dry run by default, idempotent, dedupes on domain and email."""

import aiosqlite
import pytest
import pytest_asyncio

from harvey.importer import import_csv
from harvey.state import StateManager

CSV = """Firm Name,City,Website,Email,Phone,Attorney Name
Acme Legal,Springfield,https://www.acme-legal.test/about,intake@acme-legal.test,(555) 010-0100,Jane Doe
Acme Legal,Springfield,https://www.acme-legal.test,partner@acme-legal.test,,Sam Roe Jr
Acme Legal,Springfield,https://www.acme-legal.test,INTAKE@acme-legal.test,,
Beta Law,Shelbyville,,LinkedIn-only,,
Gamma Law,Capital City,,owner@gmail.test,,
Delta Law,Ogdenville,,not-an-email@,,
,,,someone@nameless-firm.test,,
"""


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "import.db"))
    await s.init_db()
    return s


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "firms.csv"
    p.write_text(CSV, encoding="utf-8")
    return p


async def _count(state, table):
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_dry_run_reports_without_writing(state, csv_file):
    report = await import_csv(state, csv_file)
    assert report.rows == 7
    assert report.prospects_new == 4
    assert report.companies_new == 3
    assert report.rejected == {
        "duplicate email in file": 1,
        "no email (blank or placeholder)": 1,
        "malformed email": 1,
    }
    assert await _count(state, "prospects") == 0
    assert await _count(state, "companies") == 0


@pytest.mark.asyncio
async def test_apply_lands_rows_and_is_idempotent(state, csv_file):
    first = await import_csv(state, csv_file, apply=True, industry="law")
    assert (first.prospects_new, first.companies_new) == (4, 3)
    assert await _count(state, "prospects") == 4
    assert await _count(state, "companies") == 3

    jane = await state.get_prospect_by_email("intake@acme-legal.test")
    partner = await state.get_prospect_by_email("partner@acme-legal.test")
    assert (jane.first_name, jane.last_name) == ("Jane", "Doe")
    assert jane.email_status == "guess" and jane.industry == "law"
    assert partner.last_name == "Roe Jr"
    assert jane.company_id == partner.company_id

    again = await import_csv(state, csv_file, apply=True)
    assert again.prospects_new == 0
    assert again.rejected["already in database"] == 4
    assert await _count(state, "prospects") == 4
    assert await _count(state, "companies") == 3


@pytest.mark.asyncio
async def test_free_mail_without_website_gets_no_domain(state, csv_file, monkeypatch):
    monkeypatch.setattr("harvey.importer.FREE_MAIL", {"gmail.test"})
    await import_csv(state, csv_file, apply=True)
    prospect = await state.get_prospect_by_email("owner@gmail.test")
    company = await state.get_company(prospect.company_id)
    assert company.domain == "" and company.external_id.startswith("import:gammalaw")


@pytest.mark.asyncio
async def test_missing_columns_raise(state, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Firm Name,City\nAcme Legal,Springfield\n", encoding="utf-8")
    with pytest.raises(ValueError, match="email column"):
        await import_csv(state, bad)
