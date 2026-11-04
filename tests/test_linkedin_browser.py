"""Browser configuration tests: no real browser, credentials, or network."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import harvey.integrations.linkedin as li
from harvey.config import LinkedInChannelConfig


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    import playwright.async_api

    executable = tmp_path / "chrome"
    executable.touch()
    context = SimpleNamespace(new_page=AsyncMock(), cookies=AsyncMock(return_value=[]))
    browser = SimpleNamespace(version="156.0.8078.4", new_context=AsyncMock(return_value=context),
                              close=AsyncMock())
    chromium = SimpleNamespace(executable_path=str(executable), launch=AsyncMock(return_value=browser))
    pw = SimpleNamespace(chromium=chromium, stop=AsyncMock())
    monkeypatch.setattr(playwright.async_api, "async_playwright",
                        Mock(return_value=SimpleNamespace(start=AsyncMock(return_value=pw))))
    monkeypatch.setattr(li, "COOKIES_PATH", tmp_path / "cookies.json")
    monkeypatch.setattr(li, "ACTIVITY_PATH", tmp_path / "activity.json")
    monkeypatch.setattr(li.platform, "system", lambda: "Linux")
    return pw, browser


@pytest.mark.asyncio
async def test_full_chromium_new_headless_and_runtime_ua(runtime):
    pw, browser = runtime
    agent = li.LinkedInAutomation("", "")
    await agent.start()
    pw.chromium.launch.assert_awaited_once_with(
        channel="chromium", headless=True, executable_path=pw.chromium.executable_path)
    assert browser.new_context.call_args.kwargs["user_agent"] == li.USER_AGENT
    await agent.stop()
    browser.close.assert_awaited_once()
    pw.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_version_follows_browser_upgrade(runtime):
    _, browser = runtime
    browser.version = "157.0.8100.1"
    await li.LinkedInAutomation("", "").start()
    assert "Chrome/157.0.8100.1" in browser.new_context.call_args.kwargs["user_agent"]


@pytest.mark.asyncio
async def test_explicit_config_overrides(runtime, tmp_path):
    pw, browser = runtime
    binary = tmp_path / "custom-chrome"
    binary.touch()
    config = LinkedInChannelConfig(user_agent="test UA", executable_path=str(binary))
    agent = li.LinkedInAutomation("", "", user_agent=config.user_agent,
                                 executable_path=config.executable_path)
    await agent.start()
    assert pw.chromium.launch.call_args.kwargs["executable_path"] == str(binary)
    assert browser.new_context.call_args.kwargs["user_agent"] == "test UA"


@pytest.mark.asyncio
async def test_missing_full_binary_has_actionable_error_and_stops_playwright(runtime, tmp_path):
    pw, _ = runtime
    agent = li.LinkedInAutomation("", "", executable_path=str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="without --only-shell"):
        await agent.start()
    pw.chromium.launch.assert_not_awaited()
    pw.stop.assert_awaited_once()
    assert agent._pw is None


@pytest.mark.asyncio
async def test_context_failure_closes_browser(runtime):
    pw, browser = runtime
    browser.new_context.side_effect = RuntimeError("context failed")
    with pytest.raises(RuntimeError, match="LinkedIn browser startup failed"):
        await li.LinkedInAutomation("", "").start()
    browser.close.assert_awaited_once()
    pw.stop.assert_awaited_once()


@pytest.mark.parametrize("system,expected", [
    ("Linux", "X11; Linux x86_64"), ("Windows", "Windows NT 10.0; Win64; x64"),
    ("Darwin", "Macintosh; Intel Mac OS X 10_15_7"),
])
def test_ua_platform(monkeypatch, system, expected):
    monkeypatch.setattr(li.platform, "system", lambda: system)
    assert expected in li.browser_user_agent("156.0.8078.4")


def test_invalid_browser_version_is_not_silently_spoofed():
    with pytest.raises(RuntimeError, match="Cannot determine Chromium version"):
        li.browser_user_agent("unknown")
