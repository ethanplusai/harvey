"""Bing wraps its result links, and Scout has to unwrap them.

Bing's HTML results do not link to the destination: they link to
``bing.com/ck/a?...&u=a1<base64url>``. Scout took that anchor at face value, so
every result's domain read as ``bing.com`` and was discarded for not being a
company site. With Serper credits exhausted the free backends are the only
ones left, and a search that finds ten manufacturers looked exactly like a
search that found nothing.

DuckDuckGo's redirect was already unwrapped here; Bing's never was.
"""

import base64
from types import SimpleNamespace

import pytest

from sellbird.agents.scout import Scout


# A redirect exactly as Bing emitted it during a live run.
WIKIPEDIA_REDIRECT = (
    "https://www.bing.com/ck/a?!&&p=4dd037ec0dae8e4c23a094572b3cd3ac"
    "2ddf89443b997a7fd16f66d46f6c840bJmltdHM9MTc5NDYxNDQwMA&ptn=3&ver=2"
    "&hsh=4&fclid=11bfca18-0841-687f-0ef7-d209096669e4"
    "&u=a1aHR0cHM6Ly9lbi53aWtpcGVkaWEub3JnL3dpa2kvTWFudWZhY3R1cmluZw&ntb=1"
)


def _redirect_for(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc&ptn=3&u=a1{encoded}&ntb=1"


def test_a_bing_redirect_yields_the_real_url():
    assert Scout._unwrap_bing_redirect(WIKIPEDIA_REDIRECT) == (
        "https://en.wikipedia.org/wiki/Manufacturing"
    )


def test_a_bing_redirect_to_a_company_yields_that_company():
    assert Scout._unwrap_bing_redirect(
        _redirect_for("https://www.somesharkfittings.example/about")
    ) == "https://www.somesharkfittings.example/about"


def test_a_plain_url_is_left_alone():
    url = "https://acme.example/team"
    assert Scout._unwrap_bing_redirect(url) == url


def test_an_unencoded_bing_target_is_returned_as_is():
    href = "https://www.bing.com/ck/a?u=https%3A%2F%2Facme.example%2F"
    assert Scout._unwrap_bing_redirect(href) == "https://acme.example/"


def test_a_redirect_that_will_not_decode_is_left_alone():
    # Nothing recognisable in ``u``: a result we cannot decode must not
    # become a crash, and must not become a bogus domain either.
    href = "https://www.bing.com/ck/a?u=not-base64-and-no-marker"
    assert Scout._unwrap_bing_redirect(href) == href


def test_a_bing_redirect_without_a_target_is_left_alone():
    href = "https://www.bing.com/ck/a?ptn=3&ntb=1"
    assert Scout._unwrap_bing_redirect(href) == href


@pytest.mark.asyncio
async def test_a_bing_search_returns_the_real_domains_not_bing():
    scout = Scout(
        brain=None,
        state=SimpleNamespace(),
        config=SimpleNamespace(),
        env=SimpleNamespace(),
    )

    html = (
        "<html><body>"
        '<li class="b_algo"><h2><a href="'
        + _redirect_for("https://www.deccanhydraulics.example/")
        + '">Deccan Hydraulics</a></h2>'
        '<div class="b_caption"><p>Pune, India — hydraulic cylinders.</p></div></li>'
        '<li class="b_algo"><h2><a href="'
        + _redirect_for("https://nashikforge.example/contact")
        + '">Nashik Forge</a></h2>'
        '<div class="b_caption"><p>Nashik, India — forging.</p></div></li>'
        "</body></html>"
    )

    async def fetch(url, **kw):
        return SimpleNamespace(text=html)

    scout._fetch = fetch

    results = await scout._search_bing('"Manufacturing" "Nashik, India"')

    urls = [u for u, _ in results]
    assert urls == [
        "https://www.deccanhydraulics.example/",
        "https://nashikforge.example/contact",
    ]
    assert all("bing.com" not in u for u in urls)
