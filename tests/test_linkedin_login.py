"""Login-form and diagnostic regression tests.

No browser/network or persisted cookies are used.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import harvey.integrations.linkedin as li


class Locator:
    def __init__(self, elements=()):
        self.elements = list(elements)

    async def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    @property
    def first(self):
        return self.elements[0]


def element(label="", visible=True, src=""):
    return SimpleNamespace(is_visible=AsyncMock(return_value=visible),
                           inner_text=AsyncMock(return_value=label), fill=AsyncMock(),
                           click=AsyncMock(), press=AsyncMock(),
                           get_attribute=AsyncMock(return_value=src))


@pytest.fixture
def login(monkeypatch, tmp_path):
    monkeypatch.setattr(li, "ACTIVITY_PATH", tmp_path / "activity.json")
    agent = li.LinkedInAutomation("operator@example.com", "fake-password")
    email, password, apple, submit = (element(), element(), element("Iniciar sesión con Apple"),
                                     element("Iniciar sesión"))
    locators = {
        'input[type="email"]': [email],
        'input[type="password"]': [password],
        "button": [apple, submit],
        "iframe": [],
    }
    agent.page = SimpleNamespace(
        url="https://www.linkedin.com/login", goto=AsyncMock(),
        locator=lambda selector: Locator(locators.get(selector, [])))
    agent.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
    agent._wait_settled = AsyncMock()
    agent._random_delay = AsyncMock()
    agent._save_cookies_sync = Mock()
    return agent, locators


@pytest.mark.asyncio
async def test_new_email_markup_and_exact_button_not_apple(login):
    agent, fields = login
    await agent.login()
    fields['input[type="email"]'][0].fill.assert_awaited_once_with("operator@example.com")
    fields['input[type="password"]'][0].fill.assert_awaited_once_with("fake-password")
    fields["button"][0].click.assert_not_awaited()
    fields["button"][1].click.assert_awaited_once()


@pytest.mark.asyncio
async def test_fill_first_visible_skips_hidden_and_broken_elements(login):
    agent, fields = login
    broken = element()
    broken.fill.side_effect = RuntimeError("detached")
    hidden, shown = element(visible=False), element()
    fields["#username"] = [hidden, broken, shown]
    assert await agent._fill_first_visible(["#missing", "#username"], "test")
    hidden.fill.assert_not_awaited()
    shown.fill.assert_awaited_once_with("test")


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Sign in", "  Iniciar\n sesión ", "Iniciar sesion"])
async def test_button_labels(login, label):
    agent, fields = login
    fields["button"] = [element("Sign in", visible=False), element("Sign in with Apple"), element(label)]
    assert await agent._click_login_button()
    fields["button"][0].click.assert_not_awaited()
    fields["button"][1].click.assert_not_awaited()
    fields["button"][2].click.assert_awaited_once()


@pytest.mark.asyncio
async def test_enter_fallback(login):
    agent, fields = login
    fields["button"] = [element("Sign in with Apple")]
    await agent.login()
    fields['input[type="password"]'][0].press.assert_awaited_once_with("Enter")


@pytest.mark.asyncio
async def test_cookie_session_skips_form_and_resets_previous_failure(login, caplog):
    agent, fields = login
    agent.last_login_failure = "recaptcha"
    agent.page.url = "https://www.linkedin.com/feed/"
    assert await agent.login()
    assert agent.last_login_failure is None
    fields['input[type="email"]'][0].fill.assert_not_awaited()
    assert li.LINKEDIN_LOGIN_BLOCKED_RECAPTCHA not in caplog.text


@pytest.mark.asyncio
async def test_successful_submission_saves_session(login):
    agent, fields = login
    async def submit():
        agent.page.url = "https://www.linkedin.com/feed/"
    fields["button"][1].click.side_effect = submit
    fields["iframe"] = [element(src="https://www.google.com/recaptcha/enterprise/anchor", visible=False)]
    assert await agent.login()
    agent._save_cookies_sync.assert_called_once_with([])
    assert agent.last_login_failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["login", "checkpoint/challenge"])
async def test_invisible_enterprise_frame_is_stable_recaptcha_failure(login, caplog, path):
    agent, fields = login
    agent.page.url = "https://www.linkedin.com/" + path
    fields["iframe"] = [element(src="https://www.google.com/recaptcha/enterprise/anchor", visible=False)]
    assert not await agent.login()
    assert agent.last_login_failure == "recaptcha"
    assert li.LINKEDIN_LOGIN_BLOCKED_RECAPTCHA in caplog.text
    fields["iframe"][0].click.assert_not_awaited()
    assert "fake-password" not in caplog.text
    agent._save_cookies_sync.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,reason", [("login", "rejected"), ("checkpoint", "checkpoint")])
async def test_other_login_failures_not_classified_as_recaptcha(login, caplog, path, reason):
    agent, fields = login
    agent.page.url = "https://www.linkedin.com/" + path
    fields["iframe"] = [element(src="https://example.com/captcha")]
    assert not await agent.login()
    assert agent.last_login_failure == reason
    assert li.LINKEDIN_LOGIN_BLOCKED_RECAPTCHA not in caplog.text


@pytest.mark.asyncio
async def test_missing_credentials_no_recaptcha_marker(login, caplog):
    agent, _ = login
    agent.password = ""
    assert not await agent.login()
    assert agent.last_login_failure == "credentials_missing"
    assert li.LINKEDIN_LOGIN_BLOCKED_RECAPTCHA not in caplog.text


@pytest.mark.asyncio
async def test_submit_exception_still_detects_recaptcha(login, caplog):
    agent, fields = login
    agent._click_login_button = AsyncMock(side_effect=RuntimeError("fake-password"))
    fields["iframe"] = [element(src="https://www.recaptcha.net/recaptcha/enterprise/anchor")]
    assert not await agent.login()
    assert agent.last_login_failure == "recaptcha"
    assert li.LINKEDIN_LOGIN_BLOCKED_RECAPTCHA in caplog.text
    assert "fake-password" not in caplog.text
