"""LinkedIn profile-search parsing: company extraction and junk-name rejection."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harvey.agents.scout import Scout


def _scout():
    # The helpers only read class constants, so skip the heavy constructor.
    return Scout.__new__(Scout)


def _strategy_scout(domain: str) -> Scout:
    """A Scout wired for _prospect_via_profile_search with the network
    patched out: one search hit, a domain guess that returns ``domain``,
    and recording state mocks. All fictional example.com-style data."""
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    state = SimpleNamespace(
        add_prospect=AsyncMock(),
        prospect_exists=AsyncMock(return_value=False),
        log_action=AsyncMock(),
    )
    config = SimpleNamespace(
        icp=SimpleNamespace(
            geography=["India"], industries=["drone training"], titles=["Founder"]
        ),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=True)),
    )
    scout = Scout(brain, state, config, SimpleNamespace(linkedin_email="me@example.com"))
    scout._web_search = AsyncMock(return_value=[(
        "https://in.linkedin.com/in/asha-verma-examplerobotics",
        "Asha Verma - Founder - Example Robotics | LinkedIn\nFounder at Example Robotics.",
    )])
    scout._guess_domain = AsyncMock(return_value=domain)
    scout._ensure_company = AsyncMock(return_value="co_1")
    scout._resolve_email = AsyncMock(return_value=("asha@examplerobotics.com", "guess"))
    return scout


def test_company_from_dash_title():
    s = _scout()
    snippet = "Ilaya Bharathi - Founder - Kreate Labs | LinkedIn\nFounder at Kreate Labs."
    assert s._extract_linkedin_company(snippet) == "Kreate Labs"


def test_company_from_at_title():
    s = _scout()
    assert s._extract_linkedin_company("Riya Shah - Lab In-charge at JECRC University | LinkedIn") == "JECRC University"


def test_company_from_experience_field():
    s = _scout()
    snippet = "Vijayendra Singh\nExperience: Navyug Infosolutions · Education: MNIT · Location: Jaipur"
    assert s._extract_linkedin_company(snippet) == "Navyug Infosolutions"


def test_no_company_or_non_company_is_rejected():
    s = _scout()
    assert s._extract_linkedin_company("") == ""
    assert s._extract_linkedin_company("Shaju P G | LinkedIn") == ""
    assert s._extract_linkedin_company("Aman Jain - Designer - Freelance | LinkedIn") == ""


def test_title_words_are_not_names():
    s = _scout()
    assert s._looks_like_title_words({"first_name": "Startup", "last_name": "Founder"})
    assert s._looks_like_title_words({"first_name": "Senior", "last_name": "Software"})
    assert not s._looks_like_title_words({"first_name": "Surendra", "last_name": "Kankariya"})


@pytest.mark.asyncio
async def test_unresolvable_company_is_skipped():
    """A company name that resolves to no domain can never be emailed or
    verified: no prospect row, no company row."""
    scout = _strategy_scout(domain="")
    count = await scout._prospect_via_profile_search()
    assert count == 0
    scout._ensure_company.assert_not_awaited()
    scout.state.add_prospect.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolved_company_is_attached_before_saving():
    """When the name resolves, the prospect is saved with the company
    attached (company_id) and its email resolved against that domain."""
    scout = _strategy_scout(domain="examplerobotics.com")
    count = await scout._prospect_via_profile_search()
    assert count == 1
    scout._ensure_company.assert_awaited_once()
    scout._resolve_email.assert_awaited_once()
    prospect = scout.state.add_prospect.await_args.args[0]
    assert prospect.company == "Example Robotics"
    assert prospect.company_id == "co_1"
    assert prospect.email == "asha@examplerobotics.com"
    assert prospect.email_status == "guess"
    assert prospect.source == "web_search"
