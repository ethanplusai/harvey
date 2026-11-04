import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from harvey.agents.scout import Scout
from harvey.integrations.web_search import (
    filter_site_results, matches_site, parse_bing, parse_duckduckgo,
    site_targets, unwrap_bing_url, unwrap_duckduckgo_url,
)

PROFILE = "https://www.linkedin.com/in/alex-example-1234"


def bing_link(url=PROFILE):
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return "https://www.bing.com/ck/a?!&&p=test&u=a1" + encoded + "&ntb=1"


@pytest.mark.parametrize("href", [bing_link(), bing_link().replace("https:", ""),
    bing_link().replace("https://www.bing.com", ""),
    "https://www.bing.com/ck/a?u=" + quote(PROFILE, safe=""), PROFILE])
def test_bing_unwrap(href):
    assert unwrap_bing_url(href) == PROFILE


@pytest.mark.parametrize("payload", ["", "a1!bad", "a1_w", "javascript:alert(1)",
    "a1" + base64.urlsafe_b64encode(b"file:///etc/passwd").decode()])
def test_malformed_bing_tracking_is_not_a_result(payload):
    assert unwrap_bing_url("https://www.bing.com/ck/a?u=" + payload) == ""


def test_foreign_u_query_is_not_unwrapped():
    url = "https://example.com/ck/a?u=" + quote(PROFILE)
    assert unwrap_bing_url(url) == url


@pytest.mark.parametrize("value", [None, {}, 123, "https://[broken", "https://example.com/bad url"])
def test_malformed_urls_cannot_discard_other_results(value):
    assert unwrap_bing_url(value) == ""
    assert unwrap_duckduckgo_url(value) == ""
    assert filter_site_results([(value, "bad"), (PROFILE, "good")], "site:linkedin.com/in") == [(PROFILE, "good")]


@pytest.mark.parametrize("url,allowed", [
    (PROFILE, True), ("https://es.linkedin.com/in/alex-example", True),
    ("https://linkedin.com/in/alex-example?x=1", True),
    ("https://linkedin.com/company/example", False),
    ("https://linkedin.com/invalid/profile", False),
    ("https://notlinkedin.com/in/example", False),
    ("https://linkedin.com.evil.test/in/example", False),
    ("https://example.com/?url=https://linkedin.com/in/example", False),
    ("https://linkedin.com@example.com/in/example", False),
    ("javascript:alert(1)", False), ("https://[broken", False),
])
def test_site_host_and_path_boundaries(url, allowed):
    assert matches_site(url, "linkedin.com/in") is allowed


def test_site_operator_and_no_operator():
    assert site_targets('site:"linkedin.com/in" founder -site:example.com') == ["linkedin.com/in"]
    rows = [(PROFILE, "profile"), ("https://example.com", "irrelevant")]
    assert filter_site_results(rows, "SITE:https://linkedin.com/in founder") == rows[:1]
    assert filter_site_results(rows, "founder") == rows


def test_bing_html_only_organic_results_not_mentions():
    html = f'''<div>site:linkedin.com/in</div><a href="{PROFILE}">navigation</a>
    <li class="b_algo"><h2><a href="{bing_link()}">Alex Example - Founder</a></h2>
    <div class="b_caption"><p>Example industry</p></div></li>
    <li class="b_algo"><h2><a href="https://example.com">Unrelated</a></h2></li>'''
    rows = filter_site_results(parse_bing(html), "site:linkedin.com/in founder")
    assert rows == [(PROFILE, "Alex Example - Founder Example industry")]


@pytest.mark.parametrize("href", [PROFILE,
    "/l/?uddg=" + quote(PROFILE, safe=""),
    "//duckduckgo.com/l/?uddg=" + quote(PROFILE, safe="") + "&rut=123"])
def test_ddg_unwrap(href):
    assert unwrap_duckduckgo_url(href) == PROFILE


@pytest.mark.parametrize("html", [
    '<div class="result"><a class="result__a" href="{url}">Alex Example</a>'
    '<span class="result__snippet">Founder</span></div>',
    '<div class="web-result"><a class="result__a" href="{url}">Alex Example</a>'
    '<span class="result__snippet">Founder</span></div>',
    '<table><tr><td><a class="result-link" href="{url}">Alex Example</a></td></tr>'
    '<tr><td class="result-snippet">Founder</td></tr></table>',
    '<article data-testid="result"><h2><a data-testid="result-title-a" href="{url}">'
    'Alex Example</a></h2><div data-result="snippet">Founder</div></article>',
])
def test_ddg_markup_variants(html):
    redirect = "/l/?uddg=" + quote(PROFILE, safe="")
    assert parse_duckduckgo(html.format(url=redirect)) == [(PROFILE, "Alex Example Founder")]


def test_ddg_dedup_and_ignore_navigation_invalid_targets():
    html = f'<a href="{PROFILE}">nav</a><a class="result__a" href="javascript:x">bad</a>'
    html += f'<a class="result__a" href="{PROFILE}">Alex Example</a>' * 2
    assert parse_duckduckgo(html) == [(PROFILE, "Alex Example")]
    assert parse_duckduckgo('<div class="anomaly-modal">Challenge</div>') == []


@pytest.mark.asyncio
async def test_irrelevant_bing_results_do_not_stop_fallback():
    scout = Scout(None, None, SimpleNamespace(), SimpleNamespace())
    scout._search_duckduckgo = AsyncMock(return_value=[])
    scout._search_bing = AsyncMock(return_value=[("https://example.com", "HEAD")])
    scout._search_google = AsyncMock(return_value=[(PROFILE, "Alex Example")])
    assert await scout._web_search("site:linkedin.com/in founder") == [(PROFILE, "Alex Example")]
    scout._search_google.assert_awaited_once()
