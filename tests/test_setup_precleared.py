"""Tests for the optional Precleared step in `harvey setup`: off unless the
user says yes, saved answers are the defaults when setup is re-run, the key is
read with hidden input and never printed, and other settings are kept. No
network: the step only writes harvey.yaml and .env."""

import shutil
from types import SimpleNamespace

import pytest
import yaml

import harvey.setup as setup
from harvey.config import load_config
from harvey.integrations.precleared import PreclearedClient
from harvey.paths import PROJECT_ROOT

FAKE_KEY = "pc_live_example_not_a_real_key"


@pytest.fixture
def files(tmp_path, monkeypatch):
    """A copy of the shipped harvey.yaml and an empty .env, in a temp folder."""
    config = tmp_path / "harvey.yaml"
    shutil.copy(PROJECT_ROOT / "harvey.yaml", config)
    env = tmp_path / ".env"
    monkeypatch.setattr(setup, "CONFIG_FILE", config)
    monkeypatch.setattr(setup, "ENV_FILE", env)
    monkeypatch.delenv("PRECLEARED_API_KEY", raising=False)
    return SimpleNamespace(config=config, env=env)


def script(monkeypatch, yes_no, answers):
    """Answer the wizard's questions in order. None means just pressing Enter.

    Returns the questions asked, so tests can check defaults and hidden input.
    """
    asked = SimpleNamespace(yes_no=[], questions=[])

    def ask_yes_no(prompt, default=True):
        asked.yes_no.append((prompt, default))
        answer = yes_no.pop(0)
        return default if answer is None else answer

    def ask(prompt, default="", required=True, secret=False):
        asked.questions.append(SimpleNamespace(prompt=prompt, default=default, secret=secret))
        answer = answers.pop(0)
        return answer or default

    monkeypatch.setattr(setup, "_ask_yes_no", ask_yes_no)
    monkeypatch.setattr(setup, "_ask", ask)
    return asked


def set_precleared(path, **values):
    data = yaml.safe_load(path.read_text())
    data["compliance"]["precleared"].update(values)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def saved(path) -> dict:
    return yaml.safe_load(path.read_text())["compliance"]["precleared"]


def finish(wizard):
    """Write .env and harvey.yaml the way the end of `harvey setup` does."""
    if not wizard.config:
        wizard.config = {"channels": {"email": {"max_daily_sends": 50}}}
    wizard._write_env()
    wizard._write_config()


def test_check_stays_off_unless_the_user_says_yes(files, monkeypatch):
    asked = script(monkeypatch, yes_no=[None], answers=[])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert asked.yes_no == [("Turn on the cold-email check?", False)]
    assert asked.questions == []  # no country or key questions after a no
    assert wizard.precleared is None
    assert saved(files.config)["enabled"] is False
    assert "PRECLEARED_API_KEY" not in files.env.read_text()


def test_yes_saves_the_settings_and_a_free_key(files, monkeypatch, capsys):
    asked = script(monkeypatch, yes_no=[True], answers=["uk", "us", FAKE_KEY])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    key_question = asked.questions[-1]
    assert key_question.secret is True
    assert "Enter to skip" in key_question.prompt
    assert saved(files.config) == {
        "enabled": True,
        "sender_country": "GB",
        "address_source": "unknown",
        "prospect_country": "US",
    }
    assert f"PRECLEARED_API_KEY={FAKE_KEY}" in files.env.read_text().splitlines()
    assert FAKE_KEY not in capsys.readouterr().out

    # The saved config loads and switches the check on.
    client = PreclearedClient.from_config(
        load_config(str(files.config)), SimpleNamespace(precleared_api_key=FAKE_KEY))
    assert (client.sender_country, client.prospect_country, client.api_key) == ("GB", "US", FAKE_KEY)


def test_rerunning_setup_keeps_a_check_that_is_on(files, monkeypatch):
    set_precleared(files.config, enabled=True, sender_country="GB", prospect_country="US")
    files.env.write_text("PRECLEARED_API_KEY=existing_key\n")
    asked = script(monkeypatch, yes_no=[None], answers=["", "", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert asked.yes_no[0][1] is True
    assert [q.default for q in asked.questions[:2]] == ["GB", "US"]
    assert "blank to skip" not in asked.questions[0].prompt  # Enter keeps GB
    assert "Enter keeps your current key" in asked.questions[2].prompt
    assert saved(files.config)["enabled"] is True
    assert saved(files.config)["prospect_country"] == "US"
    assert "PRECLEARED_API_KEY=existing_key" in files.env.read_text().splitlines()


def test_no_switches_off_a_check_that_was_on_and_keeps_its_settings(files, monkeypatch):
    set_precleared(files.config, enabled=True, sender_country="US")
    script(monkeypatch, yes_no=[False], answers=[])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert saved(files.config)["enabled"] is False
    assert saved(files.config)["sender_country"] == "US"


def test_sender_country_must_be_gb_or_us_and_blank_skips(files, monkeypatch, capsys):
    asked = script(monkeypatch, yes_no=[True], answers=["DE", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert len(asked.questions) == 2  # asked again after DE, then skipped: no key question
    assert "type GB or US" in capsys.readouterr().out
    assert wizard.precleared is None
    assert saved(files.config)["enabled"] is False


def test_prospect_country_is_checked_and_norway_stays_a_string(files, monkeypatch, capsys):
    script(monkeypatch, yes_no=[True], answers=["GB", "Norway", "no", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert "not a valid prospect_country" in capsys.readouterr().out
    # YAML would read an unquoted NO as false; the saved value must stay "NO".
    assert saved(files.config)["prospect_country"] == "NO"
    assert load_config(str(files.config)).compliance.precleared.prospect_country == "NO"


def test_none_clears_a_saved_prospect_country(files, monkeypatch):
    set_precleared(files.config, enabled=True, sender_country="GB", prospect_country="US")
    asked = script(monkeypatch, yes_no=[None], answers=["", "none", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert "type none" in asked.questions[1].prompt
    assert saved(files.config)["prospect_country"] == ""
    assert saved(files.config)["enabled"] is True


@pytest.mark.parametrize("env_text", ["", 'PRECLEARED_API_KEY=""\n'])
def test_turning_it_on_without_a_key_leaves_a_placeholder(files, monkeypatch, capsys, env_text):
    files.env.write_text(env_text)
    asked = script(monkeypatch, yes_no=[True], answers=["US", "", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert "Enter to skip" in asked.questions[-1].prompt  # an empty value is no key
    assert any(line in ("PRECLEARED_API_KEY=", 'PRECLEARED_API_KEY=""')
               for line in files.env.read_text().splitlines())
    assert "10 checks a day" in capsys.readouterr().out
    assert saved(files.config)["enabled"] is True


def test_a_new_config_file_gets_the_check(tmp_path, monkeypatch):
    config = tmp_path / "harvey.yaml"  # not created yet
    monkeypatch.setattr(setup, "CONFIG_FILE", config)
    monkeypatch.setattr(setup, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("PRECLEARED_API_KEY", raising=False)
    script(monkeypatch, yes_no=[True], answers=["GB", "", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert saved(config) == {"enabled": True, "sender_country": "GB", "prospect_country": ""}


@pytest.mark.asyncio
async def test_setup_asks_about_the_check_before_writing_anything(monkeypatch, capsys):
    calls = []
    wizard = setup.SetupWizard()

    def record(name, is_async=False):
        async def async_step(*a, **k):
            calls.append(name)

        def step(*a, **k):
            calls.append(name)

        return async_step if is_async else step

    for name in ("_check_prerequisites", "_setup_email_platform", "_setup_product",
                 "_print_summary"):
        monkeypatch.setattr(wizard, name, record(name, is_async=True))
    for name in ("_setup_linkedin", "_setup_cloudflare", "_setup_behavior",
                 "_setup_compliance", "_write_env", "_write_config"):
        monkeypatch.setattr(wizard, name, record(name))

    await wizard.run()

    assert calls.index("_setup_behavior") < calls.index("_setup_compliance") < calls.index("_write_env")
    assert "Step 7/7: Cold-Email Check (Optional)" in capsys.readouterr().out
