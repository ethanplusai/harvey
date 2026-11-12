"""SITE_PAGE_COUNT counts pages, not the sitemaps of a sitemap index.

Most sites (WordPress with an SEO plugin, for one) publish /sitemap.xml as an
index of sitemaps. Counting its entries called a site of fifty pages in three
sitemaps a three-page site, which then reached the writer as "a very small
site". The collector now follows the index into the site's own sitemaps.
"""

import os
import tempfile

import pytest
import pytest_asyncio

from sellbird.collectors.profile import MAX_INDEXED_SITEMAPS, ProfileCollector
from sellbird.models.company import Company
from sellbird.signals import seed_signal_catalog
from sellbird.state import StateManager

HOME = "<html><body><h1>Acme Bakery</h1></body></html>"


def urlset(n, prefix="page"):
    locs = "".join(f"<url><loc>https://acme.example/{prefix}{i}</loc></url>" for i in range(n))
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


def index(*children):
    locs = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</sitemapindex>'


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "p.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        await sm.set_signal_status("SITE_PAGE_COUNT", "confirmed")
        yield sm


async def page_count(state, files):
    """SITE_PAGE_COUNT observed for a site whose files are {url: body}."""
    c = ProfileCollector.__new__(ProfileCollector)
    c.confirmed = await state.confirmed_signal_codes()
    c.site_names = {}
    c.state = state
    fetched = []

    async def fake_get(url):
        fetched.append(url)
        return files.get(url, HOME if url.rstrip("/") == "https://acme.example" else "")

    c._get = fake_get
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    obs = await c.profile_company(cid, "acme.example")
    counts = [o["value_num"] for o in obs if o["signal_code"] == "SITE_PAGE_COUNT"]
    return (counts[0] if counts else None), fetched


@pytest.mark.asyncio
async def test_a_plain_sitemap_counts_its_pages(state):
    got, _ = await page_count(state, {"https://acme.example/sitemap.xml": urlset(7)})
    assert got == 7


@pytest.mark.asyncio
async def test_an_index_counts_the_pages_inside_not_its_entries(state):
    files = {
        "https://acme.example/sitemap.xml": index(
            "https://acme.example/page-sitemap.xml", "https://acme.example/post-sitemap.xml",
            "https://acme.example/product-sitemap.xml"),
        "https://acme.example/page-sitemap.xml": urlset(25),
        "https://acme.example/post-sitemap.xml": urlset(10, "post"),
        "https://acme.example/product-sitemap.xml": urlset(24, "item"),
    }
    got, _ = await page_count(state, files)
    assert got == 59            # not 3


@pytest.mark.asyncio
async def test_an_index_whose_sitemaps_cannot_be_read_records_nothing(state):
    files = {"https://acme.example/sitemap.xml": index("https://acme.example/page-sitemap.xml")}
    got, _ = await page_count(state, files)
    assert got is None          # better no number than a wrong one


@pytest.mark.asyncio
async def test_a_sitemap_on_another_host_is_never_fetched(state):
    files = {
        "https://acme.example/sitemap.xml": index(
            "https://elsewhere.example/page-sitemap.xml", "https://acme.example/page-sitemap.xml"),
        "https://acme.example/page-sitemap.xml": urlset(4),
        "https://elsewhere.example/page-sitemap.xml": urlset(900),
    }
    got, fetched = await page_count(state, files)
    assert got == 4
    assert "https://elsewhere.example/page-sitemap.xml" not in fetched


@pytest.mark.asyncio
async def test_only_the_first_sitemaps_of_a_huge_index_are_read(state):
    kids = [f"https://acme.example/s{i}.xml" for i in range(MAX_INDEXED_SITEMAPS + 5)]
    files = {"https://acme.example/sitemap.xml": index(*kids)}
    files.update({k: urlset(2) for k in kids})
    got, fetched = await page_count(state, files)
    assert got == 2 * MAX_INDEXED_SITEMAPS
    assert kids[-1] not in fetched
