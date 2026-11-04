from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from harvey.agents.scout import Scout
from harvey.config import EnvConfig, SearchConfig, load_config

PROFILE = "https://www.linkedin.com/in/alex-example"


def scout_for(**settings):
    return Scout(None, None, SimpleNamespace(search=SearchConfig(**settings)), EnvConfig())


def test_yaml_search_config_roundtrip(tmp_path):
    from harvey.paths import PROJECT_ROOT
    import yaml
    data = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text())
    data["search"] = {"engine": "SERPER", "api_key": " fake-test-key ", "fallback": False}
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump(data))
    config = load_config(str(path)).search
    assert config.engine == "serper"
    assert config.api_key.get_secret_value() == "fake-test-key"
    assert "fake-test-key" not in repr(config)
    assert not config.fallback


def test_invalid_engine_fails_early():
    with pytest.raises(ValidationError):
        SearchConfig(engine="made-up")


@pytest.mark.asyncio
async def test_auto_prefers_configured_serper_key_and_retains_legacy_env():
    scout = scout_for(api_key="fake-config-key")
    scout.env.serper_api_key = "fake-env-key"
    scout._search_serper = AsyncMock(return_value=[(PROFILE, "Alex Example")])
    scout._search_duckduckgo = AsyncMock()
    assert await scout._web_search("site:linkedin.com/in founder")
    scout._search_serper.assert_awaited_once_with("site:linkedin.com/in founder", "fake-config-key")
    scout._search_duckduckgo.assert_not_awaited()
    scout.config.search = SearchConfig()
    assert scout._search_backends()[0] == ("serper", "fake-env-key")


@pytest.mark.asyncio
async def test_explicit_engine_and_no_fallback():
    scout = scout_for(engine="bing", fallback=False)
    scout.env.serper_api_key = "fake-env-key"
    scout._search_bing = AsyncMock(return_value=[])
    scout._search_serper = AsyncMock()
    scout._search_google = AsyncMock()
    assert await scout._web_search("site:linkedin.com/in founder") == []
    scout._search_bing.assert_awaited_once()
    scout._search_serper.assert_not_awaited()
    scout._search_google.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_key_is_configuration_failure_not_empty_search(caplog):
    scout = scout_for(engine="serper")
    scout._fetch = AsyncMock()
    assert await scout._web_search("example") == []
    assert "SCOUT_SEARCH_CONFIG_ERROR engine=serper reason=missing_api_key" in caplog.text
    scout._fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_ddg_then_irrelevant_bing_then_serp_result(monkeypatch, caplog):
    import harvey.agents.scout as module
    scout = scout_for()
    requests = []

    class Client:
        async def request(self, method, url, **kw):
            requests.append(kw["headers"])
            if "duckduckgo.com" in url:
                return httpx.Response(202, text='<div class="anomaly-modal">Blocked</div>')
            if "bing.com" in url:
                return httpx.Response(200, text='<li class="b_algo"><h2>'
                                      '<a href="https://example.com">HEAD</a></h2></li>')
            return httpx.Response(200, text=f'<div class="g"><a href="{PROFILE}">'
                                  'Alex Example</a><div class="VwiC3b">Founder</div></div>')

    @asynccontextmanager
    async def client(**kw):
        yield Client()

    monkeypatch.setattr(module, "public_http_client", client)
    assert await scout._web_search("site:linkedin.com/in founder") == [(PROFILE, "Founder")]
    assert "SCOUT_SEARCH_BLOCKED engine=duckduckgo" in caplog.text
    assert "SCOUT_SEARCH_FILTERED_OUT engine=bing" in caplog.text
    assert len(requests) == 3  # challenge is not retried
    assert all(h["User-Agent"] == SearchConfig().user_agent for h in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [202, 200, 403])
async def test_anomaly_modal_is_block_not_parser_failure(monkeypatch, caplog, status):
    import harvey.agents.scout as module
    scout = scout_for(engine="duckduckgo", fallback=False)
    client = SimpleNamespace(request=AsyncMock(return_value=httpx.Response(
        status, text='<div class="anomaly-modal">Challenge</div>')))

    @asynccontextmanager
    async def factory(**kw):
        yield client

    monkeypatch.setattr(module, "public_http_client", factory)
    assert await scout._web_search("example") == []
    assert "SCOUT_SEARCH_BLOCKED engine=duckduckgo" in caplog.text
    client.request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("html,reason", [
    ("<div>New unknown markup</div>", "SCOUT_SEARCH_ERROR engine=bing reason=unrecognized_html"),
    ("<div>No results found</div>", "SCOUT_SEARCH_EMPTY engine=bing"),
])
async def test_empty_and_unrecognized_markup_are_distinct(html, reason, caplog):
    caplog.set_level("INFO")
    scout = scout_for(engine="bing", fallback=False)
    scout._fetch = AsyncMock(return_value=SimpleNamespace(text=html))
    assert await scout._web_search("example") == []
    assert reason in caplog.text


@pytest.mark.asyncio
async def test_serper_response_and_payload(caplog):
    scout = scout_for(engine="serper", api_key="fake-key", fallback=False)
    scout._fetch = AsyncMock(return_value=httpx.Response(200, json={"organic": [
        {"link": PROFILE, "title": "Alex Example", "snippet": "Founder"},
        {"link": "https://example.com", "title": "Unrelated"},
    ]}))
    assert await scout._web_search("site:linkedin.com/in founder") == [(PROFILE, "Alex Example Founder")]
    args = scout._fetch.call_args.kwargs
    assert args["json_body"]["num"] <= 10
    assert args["headers"]["X-API-KEY"] == "fake-key"
    assert "fake-key" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(200, text="oops"), httpx.Response(200, json={"error": "bad"})])
async def test_serper_invalid_response_is_not_empty(response, caplog):
    scout = scout_for(engine="serper", api_key="fake-key", fallback=False)
    scout._fetch = AsyncMock(return_value=response)
    assert await scout._web_search("example") == []
    assert "SCOUT_SEARCH_ERROR engine=serper reason=invalid_" in caplog.text


@pytest.mark.asyncio
async def test_backend_exception_does_not_log_keys_or_abort_fallback(caplog):
    scout = scout_for(engine="serper", api_key="fake-sensitive-value")
    scout._search_serper = AsyncMock(side_effect=RuntimeError("fake-sensitive-value"))
    scout._search_duckduckgo = AsyncMock(return_value=[(PROFILE, "Alex Example")])
    assert await scout._web_search("example")
    assert "fake-sensitive-value" not in caplog.text
    assert "SCOUT_SEARCH_ERROR engine=serper reason=backend_exception" in caplog.text
