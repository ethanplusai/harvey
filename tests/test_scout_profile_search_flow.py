"""Run real profile discovery with fake HTTP and fake LinkedIn login."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from harvey.agents.scout import Scout
from harvey.config import ChannelsConfig, EnvConfig, SearchConfig
from tests.test_scout_search_parsers import PROFILE, bing_link


def make_scout(email=""):
    state = SimpleNamespace(add_prospect=AsyncMock(), prospect_exists=AsyncMock(return_value=False),
                            log_action=AsyncMock())
    config = SimpleNamespace(
        channels=ChannelsConfig(linkedin={"enabled": True}),
        search=SearchConfig(engine="bing", fallback=False),
        icp=SimpleNamespace(titles=["Founder"], geography=[], industries=[]),
    )
    scout = Scout(SimpleNamespace(load_skills_for_agent=lambda name: ""), state, config,
                  EnvConfig(linkedin_email=email))
    for strategy in ("hunter", "known_companies", "hiring_boards", "company_discovery"):
        setattr(scout, "_prospect_via_" + strategy, AsyncMock(return_value=0))
    html = f'<li class="b_algo"><h2><a href="{bing_link()}">Alex Example - Founder</a></h2></li>'
    scout._fetch = AsyncMock(return_value=httpx.Response(200, text=html))
    return scout


@pytest.mark.asyncio
async def test_real_profile_strategy_without_login_or_browser(monkeypatch):
    scout = make_scout()
    def forbidden(*args, **kw):
        raise AssertionError("Public profile search must not create a browser")
    monkeypatch.setattr("harvey.agents.scout.LinkedInAutomation", forbidden)
    await scout.run()
    prospect = scout.state.add_prospect.call_args.args[0]
    assert prospect.linkedin_url == PROFILE
    assert prospect.first_name == "Alex"
    assert prospect.last_name == "Example"
    assert prospect.email == ""
    assert not prospect.email_verified
    assert scout.state.log_action.call_args.kwargs["details"] == {"prospects_found": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["recaptcha", "exception", "startup_error"])
async def test_linkedin_failure_does_not_abort_real_web_profile_search(monkeypatch, mode):
    scout = make_scout("operator@example.com")
    linkedin = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), login=AsyncMock(return_value=False),
                               last_login_failure="recaptcha")
    if mode == "exception":
        linkedin.login.side_effect = RuntimeError("login failed")
    if mode == "startup_error":
        linkedin.start.side_effect = RuntimeError("full Chromium missing")
    monkeypatch.setattr("harvey.agents.scout.LinkedInAutomation", lambda *args, **kw: linkedin)
    await scout.run()
    linkedin.stop.assert_awaited_once()
    assert scout.state.add_prospect.call_args.args[0].linkedin_url == PROFILE
    scout._prospect_via_company_discovery.assert_awaited_once()
    assert scout.state.log_action.call_args.kwargs["details"]["prospects_found"] == 1


@pytest.mark.asyncio
async def test_profile_search_rejects_foreign_urls_and_deduplicates_tracking():
    scout = make_scout()
    scout._web_search = AsyncMock(return_value=[
        ("https://evil.test/in/alex-example", "Alex Example"),
        ("https://linkedin.com/in/alex-example/recent-activity", "Alex Example"),
        (PROFILE + "?trk=first", "Alex Example"),
        (PROFILE + "/?trk=second", "Alex Example"),
    ])
    assert await scout._prospect_via_profile_search() == 1
    scout.state.add_prospect.assert_awaited_once()
    assert scout.state.add_prospect.call_args.args[0].linkedin_url == PROFILE
