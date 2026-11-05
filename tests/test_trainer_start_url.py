"""`harvey train <url>` starts on the page it was given, not the homepage.

A product can live on its own path. With a low page limit, starting at `/`
meant Harvey learned the homepage and never reached the product page.
"""

import httpx
import pytest

import harvey.trainer as trainer_mod
from harvey.config import BrainConfig

PRODUCT_URL = "https://example.com/products/widget"


def _site(request: httpx.Request) -> httpx.Response:
    body = {
        "/": "<html><body><p>Example Co home</p><a href='/about'>About</a></body></html>",
        "/products/widget": "<html><body><p>Widget product page</p></body></html>",
    }.get(request.url.path)
    if body is None:
        return httpx.Response(404)
    return httpx.Response(200, html=body)


@pytest.mark.asyncio
async def test_fallback_crawler_fetches_the_named_page_first(monkeypatch):
    monkeypatch.setattr(
        trainer_mod, "public_http_client",
        lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(_site), **kw),
    )
    pages = await trainer_mod.FallbackCrawler().crawl(
        "https://example.com", max_pages=1, start_url=PRODUCT_URL)
    assert list(pages) == [PRODUCT_URL]
    assert "Widget product page" in pages[PRODUCT_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("cloudflare", [False, True])
async def test_train_passes_the_full_url_to_the_crawler(monkeypatch, cloudflare):
    seen = {}

    async def fake_crawl(self, url, max_pages=100, **kw):
        seen["start"] = kw.get("start_url") or url
        return {}  # train() stops right after an empty crawl

    class NoDB:
        async def init_db(self):
            pass

    monkeypatch.setattr(trainer_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct" if cloudflare else "")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token" if cloudflare else "")
    crawler = trainer_mod.CloudflareCrawler if cloudflare else trainer_mod.FallbackCrawler
    monkeypatch.setattr(crawler, "crawl", fake_crawl)

    trainer = trainer_mod.Trainer(brain_config=BrainConfig())
    trainer.state = NoDB()
    assert await trainer.train(PRODUCT_URL, max_pages=1) is None
    assert seen["start"] == PRODUCT_URL
