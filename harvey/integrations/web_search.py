"""Pure search-result parsing. No HTTP, browser, credentials, or side effects."""

import base64
import binascii
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup


def _web_url(value: str) -> bool:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlparse(value)
        return bool(parsed.scheme in ("http", "https") and parsed.hostname
                    and not parsed.username and not parsed.password)
    except ValueError:
        return False


def unwrap_bing_url(href: str) -> str:
    """Decode Bing /ck/a u=a1<base64url>, including unpadded payloads.

    Invalid tracking destinations are dropped, never returned as results.
    A foreign host's `u` parameter is not a Bing redirect.
    """
    if not isinstance(href, str):
        return ""
    try:
        href = urljoin("https://www.bing.com", href)
    except ValueError:
        return ""
    if not _web_url(href):
        return ""
    parsed = urlparse(href)
    if parsed.hostname not in ("bing.com", "www.bing.com") or parsed.path.rstrip("/") != "/ck/a":
        return href
    target = parse_qs(parsed.query).get("u", [""])[0]
    if target.startswith("a1"):
        payload = target[2:]
        try:
            target = base64.b64decode(
                payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True,
            ).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            return ""
    return target if _web_url(target) else ""


def unwrap_duckduckgo_url(href: str) -> str:
    if not isinstance(href, str):
        return ""
    try:
        href = urljoin("https://duckduckgo.com", href)
    except ValueError:
        return ""
    if not _web_url(href):
        return ""
    parsed = urlparse(href)
    host = parsed.hostname or ""
    if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target if _web_url(target) else ""
    return href


def site_targets(query: str) -> list[str]:
    """Positive site: operators, with optional scheme, path and quotes."""
    return [m.group(1).strip('"\'').rstrip("/)") for m in re.finditer(
        r"(?<![\w-])\bsite:\s*([^\s]+)", query, re.IGNORECASE,
    )]


def matches_site(url: str, target: str) -> bool:
    """Match host boundaries and optional path boundaries, never substrings."""
    if not _web_url(url):
        return False
    target = target.strip('"\'')
    try:
        wanted = urlparse(target if "://" in target else "https://" + target)
        actual = urlparse(url)
        host = (actual.hostname or "").lower().rstrip(".")
        domain = (wanted.hostname or "").lower().rstrip(".")
        if not domain or not (host == domain or host.endswith("." + domain)):
            return False
        path = unquote(wanted.path).rstrip("/")
        candidate = unquote(actual.path)
        return not path or candidate == path or candidate.startswith(path + "/")
    except ValueError:
        return False


def filter_site_results(results: list[tuple[str, str]], query: str) -> list[tuple[str, str]]:
    targets = site_targets(query)
    return [(url, snippet) for url, snippet in results if _web_url(url)
            and (not targets or any(matches_site(url, target) for target in targets))]


def parse_bing(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for item in soup.select("li.b_algo"):
        link = item.select_one("h2 a[href]")
        if link is None:
            continue
        url = unwrap_bing_url(link.get("href", ""))
        if url:
            snippet = item.select_one(".b_caption p, .b_snippet, p")
            text = " ".join(el.get_text(" ", strip=True) for el in (link, snippet) if el)
            results.append((url, text))
    return results


def parse_duckduckgo(html: str) -> list[tuple[str, str]]:
    """HTML/classic, Lite table, and semantic result cards.

    Anchor-based traversal does not depend on the old `.result` wrapper.
    Only result-title anchors count: nav/citation/ad links are not profiles.
    """
    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()
    for link in soup.select("a.result__a[href], a.result-link[href], "
                            "a[data-testid='result-title-a'][href]"):
        url = unwrap_duckduckgo_url(link.get("href", ""))
        if not url or url in seen:
            continue
        container = link.find_parent(
            lambda tag: tag.name in ("article", "tr")
            or "result" in tag.get("class", [])
            or "web-result" in tag.get("class", [])
            or tag.get("data-testid") == "result"
        )
        snippet = None
        if container:
            snippet = container.select_one(
                ".result__snippet, .result-snippet, [data-result='snippet'], "
                "[data-testid='result-snippet']")
            if container.name == "tr" and snippet is None:
                # Lite's snippet is in a following row, before the next title.
                for row in container.find_next_siblings("tr", limit=3):
                    if row.select_one("a.result-link"):
                        break
                    snippet = row.select_one(".result-snippet")
                    if snippet:
                        break
        if snippet is None and link.parent:
            snippet = link.parent.select_one(".result__snippet")
        text = " ".join(el.get_text(" ", strip=True) for el in (link, snippet) if el)
        results.append((url, text))
        seen.add(url)
    return results
