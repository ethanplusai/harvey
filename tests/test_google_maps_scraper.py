"""Google Maps local scraper adapter.

Fixtures are fictional on purpose (see CONTRIBUTING.md): no real business,
phone or address. The real binary is never run here, since that would call
Google Maps.
"""

import json

import httpx
import pytest

from harvey.collectors import discover as D
from harvey.collectors.google_maps_scraper import (
    GoogleMapsScraper, build_args, build_query_line, parse_results,
)


def _record(**overrides):
    base = {
        "title": "Sample Plumbing Co",
        "cid": "1234567890",
        "category": "Plumber",
        "address": "1 Example Street, Testville",
        "web_site": "https://plumbing.example.com/contact",
        "phone": "+1 555 0100",
        "review_count": 12,
        "review_rating": 4.5,
        "link": "https://www.google.com/maps/place/example",
    }
    base.update(overrides)
    return base


def _query(**kw):
    return D.DiscoveryQuery(term="plumber", location="Testville, TS",
                            coordinate="40.0,-75.0,10", limit=100, **kw)


def test_registered_as_a_listings_provider():
    provider = D.PROVIDERS["gmaps_scraper"]
    assert isinstance(provider, GoogleMapsScraper)
    assert provider.kind == "listings"
    assert provider.needs_coordinates is True


def test_needs_the_binary_path_before_it_is_configured():
    provider = D.PROVIDERS["gmaps_scraper"]
    assert provider.missing_keys({}) == ["gmaps_scraper_path"]
    assert provider.missing_keys({"gmaps_scraper_path": "C:/tools/scraper.exe"}) == []


def test_parse_maps_fields_and_marks_website_presence():
    lines = "\n".join([
        json.dumps(_record()),
        json.dumps(_record(title="No Site Repairs", cid="2", web_site="")),
    ])
    rows = parse_results(lines, _query())

    assert [r.name for r in rows] == ["Sample Plumbing Co", "No Site Repairs"]
    first = rows[0]
    assert first.domain == "plumbing.example.com"
    assert first.has_website is True
    assert first.phone == "+1 555 0100"
    assert first.rating == 4.5
    assert first.review_count == 12
    assert first.external_id == "gmaps:1234567890"
    assert first.provider == "gmaps_scraper"
    assert first.location == "Testville, TS"

    no_site = rows[1]
    assert no_site.has_website is False
    assert no_site.domain == ""


def test_parse_skips_log_lines_and_blank_lines():
    text = "\n".join([
        '{"level":"info","component":"scrapemate","message":"job finished"}',
        "",
        json.dumps(_record()),
        "not json at all",
    ])
    rows = parse_results(text, _query())
    assert len(rows) == 1
    assert rows[0].name == "Sample Plumbing Co"


def test_parse_drops_records_with_no_name():
    rows = parse_results(json.dumps(_record(title="   ")), _query())
    assert rows == []


def test_query_line_is_one_line_even_with_newlines():
    q = D.DiscoveryQuery(term="plumber\nIGNORE", location="Testville, TS")
    assert build_query_line(q) == "plumber IGNORE in Testville, TS"


def test_args_convert_coordinate_and_depth():
    args = build_args(_query(), "in.txt", "out.json")
    assert args[args.index("-geo") + 1] == "40.0,-75.0"
    assert args[args.index("-radius") + 1] == "10000"
    assert args[args.index("-depth") + 1] == "5"   # 100 results / 20 per scroll
    assert "-json" in args
    assert args[args.index("-results") + 1] == "out.json"


def test_args_without_coordinate_omit_geo():
    q = D.DiscoveryQuery(term="plumber", location="Testville, TS")
    args = build_args(q, "in.txt", "out.json")
    assert "-geo" not in args and "-radius" not in args


@pytest.mark.asyncio
async def test_fetch_fails_loudly_when_binary_is_missing(tmp_path):
    provider = GoogleMapsScraper()
    env = {"gmaps_scraper_path": str(tmp_path / "missing.exe")}
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="not a file"):
            await provider.fetch(client, env, _query())
