from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harvey.agents.scout import Scout

STRATEGIES = (
    "hunter", "known_companies", "linkedin",
    "hiring_boards", "profile_search", "company_discovery",
)


def _scout(linkedin_enabled: bool) -> Scout:
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    state = SimpleNamespace(log_action=AsyncMock())
    config = SimpleNamespace(
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=linkedin_enabled))
    )
    env = SimpleNamespace(linkedin_email="")
    scout = Scout(brain, state, config, env)
    for name in STRATEGIES:
        setattr(scout, f"_prospect_via_{name}", AsyncMock(return_value=0))
    return scout


@pytest.mark.asyncio
async def test_profile_search_rides_the_linkedin_switch():
    """site:linkedin.com/in searches yield names with no company or email,
    which Harvey can never verify or send to, so they are off by default."""
    scout = _scout(linkedin_enabled=False)
    await scout.run()
    scout._prospect_via_profile_search.assert_not_awaited()
    scout._prospect_via_company_discovery.assert_awaited_once()

    scout = _scout(linkedin_enabled=True)
    await scout.run()
    scout._prospect_via_profile_search.assert_awaited_once()
