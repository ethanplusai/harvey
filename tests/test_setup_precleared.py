"""Tests for the optional Precleared step in `harvey setup`: off unless the
user says yes, saved answers are the defaults when setup is re-run, a free key
can be had for an email address, a pasted key is read with hidden input, no
key is ever printed, and other settings are kept. No network: the free-key
request is replaced in every test."""

import os
import shutil
from types import SimpleNamespace

import pytest
import yaml

import harvey.setup as setup
from harvey.config import load_config
from harvey.integrations.precleared import NewKey, PreclearedClient
from harvey.paths import PROJECT_ROOT

FAKE_KEY = "pc_live_example_not_a_real_key"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that would ask Precleared for a real key."""
    def refuse(*args, **kwargs):
        raise AssertionError("setup tried to reach Precleared")

    monkeypatch.setattr(setup, "create_free_key", refuse)


def free_key(monkeypatch, *results):
    """Precleared's answers to free-key requests, in order. Returns the calls."""
    calls = []
    answers = list(results)

    def create(email, country=""):
        calls.append((email, country))
        return answers.pop(0)

    monkeypatch.setattr(setup, "create_free_key", create)
    return calls


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
    asked = script(monkeypatch, yes_no=[True, False], answers=["uk", "us", FAKE_KEY])
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
    assert len(asked.yes_no) == 1  # no free key offered over a saved one
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
    # "no" is also Norway's code, so it's confirmed before being saved.
    script(monkeypatch, yes_no=[True, True, False], answers=["GB", "Norway", "no", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert "not a valid prospect_country" in capsys.readouterr().out
    # YAML would read an unquoted NO as false; the saved value must stay "NO".
    assert saved(files.config)["prospect_country"] == "NO"
    assert load_config(str(files.config)).compliance.precleared.prospect_country == "NO"


def test_answering_no_to_the_prospect_country_does_not_pick_norway(files, monkeypatch):
    # The question reads "if they're all in one", so "no" usually means "they
    # aren't". Saving it as NO would tell Precleared they're all in Norway.
    asked = script(monkeypatch, yes_no=[True, None, False], answers=["US", "no", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert asked.yes_no[1][1] is False  # Enter means not Norway
    assert saved(files.config)["prospect_country"] == ""
    assert saved(files.config)["enabled"] is True


def test_none_clears_a_saved_prospect_country(files, monkeypatch):
    set_precleared(files.config, enabled=True, sender_country="GB", prospect_country="US")
    asked = script(monkeypatch, yes_no=[None, False], answers=["", "none", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert "type none" in asked.questions[1].prompt
    assert saved(files.config)["prospect_country"] == ""
    assert saved(files.config)["enabled"] is True


@pytest.mark.parametrize("env_text", ["", 'PRECLEARED_API_KEY=""\n'])
def test_turning_it_on_without_a_key_leaves_a_placeholder(files, monkeypatch, capsys, env_text):
    files.env.write_text(env_text)
    asked = script(monkeypatch, yes_no=[True, False], answers=["US", "", ""])
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
    script(monkeypatch, yes_no=[True, False], answers=["GB", "", ""])
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


def test_saving_the_key_keeps_other_env_lines_and_stays_private(files, monkeypatch, capsys, caplog):
    other = ('SMTP_PASSWORD=a=b=c\n'
             'GMAIL_CLIENT_SECRET="quoted # not a comment"\n'
             "HUNTER_API_KEY='single'\n"
             'SERPER_API_KEY=abc#def')  # no trailing newline
    files.env.write_text(other)
    script(monkeypatch, yes_no=[True, False], answers=["US", "", FAKE_KEY])
    wizard = setup.SetupWizard()
    caplog.set_level("DEBUG")
    wizard._setup_compliance()
    finish(wizard)

    lines = files.env.read_text().splitlines()
    for line in other.splitlines():
        assert line in lines
    assert f"PRECLEARED_API_KEY={FAKE_KEY}" in lines
    if os.name != "nt":   # Windows uses the folder's ACLs, not POSIX modes
        assert files.env.stat().st_mode & 0o777 == 0o600
    assert FAKE_KEY not in capsys.readouterr().out
    assert FAKE_KEY not in caplog.text


# ── a free key for an email address ──

OFFER = "Get a free Precleared key now? It only needs your email address"


def test_setup_gets_a_free_key_for_an_email_address(files, monkeypatch, capsys, caplog):
    calls = free_key(monkeypatch, NewKey(key=FAKE_KEY))
    asked = script(monkeypatch, yes_no=[True, None], answers=["GB", "", "jo@example.com"])
    wizard = setup.SetupWizard()
    caplog.set_level("DEBUG")
    wizard._setup_compliance()
    finish(wizard)

    assert asked.yes_no[1] == (OFFER, True)  # Enter means yes
    email_question = asked.questions[-1]
    assert "Email address" in email_question.prompt and email_question.secret is False
    assert len(asked.questions) == 3  # no paste question once the key came back
    assert calls == [("jo@example.com", "GB")]
    assert f"PRECLEARED_API_KEY={FAKE_KEY}" in files.env.read_text().splitlines()
    out = capsys.readouterr().out
    assert "100 a day for its first 14 days" in out
    assert "precleared.dev/privacy" in out
    assert "Got your free key and saved it to .env." in out
    assert FAKE_KEY not in out
    assert FAKE_KEY not in caplog.text


def test_a_key_precleared_cannot_make_falls_back_to_pasting_one(files, monkeypatch, capsys):
    calls = free_key(monkeypatch, NewKey(error="This email address already has three keys"))
    asked = script(monkeypatch, yes_no=[True, True], answers=["US", "", "jo@example.com", FAKE_KEY])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "Couldn't get a key: This email address already has three keys." in out
    assert "paste one from https://precleared.dev/get-key" in out
    paste = asked.questions[-1]
    assert paste.secret is True and "Enter to skip" in paste.prompt
    assert f"PRECLEARED_API_KEY={FAKE_KEY}" in files.env.read_text().splitlines()


def test_a_mistyped_address_is_asked_for_again(files, monkeypatch, capsys):
    calls = free_key(monkeypatch,
                     NewKey(error="that doesn't look like an email address", bad_email=True),
                     NewKey(key=FAKE_KEY))
    script(monkeypatch, yes_no=[True, True], answers=["GB", "", "jo-at-example.com", "jo@example.com"])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert [email for email, _ in calls] == ["jo-at-example.com", "jo@example.com"]
    assert "paste one" not in capsys.readouterr().out
    assert f"PRECLEARED_API_KEY={FAKE_KEY}" in files.env.read_text().splitlines()


def test_a_blank_address_skips_to_pasting_a_key(files, monkeypatch, capsys):
    # The no_network guard fails the test if Precleared is asked.
    asked = script(monkeypatch, yes_no=[True, True], answers=["GB", "", "", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert asked.questions[-1].secret is True  # the paste question came next
    assert "harvey precleared-key" in capsys.readouterr().out
    assert saved(files.config)["enabled"] is True


# ── setup saves to the file Harvey reads, and says when it couldn't ──

@pytest.fixture
def trained(files):
    """`harvey train` wrote harvey.local.yaml, which wins over harvey.yaml."""
    local = files.config.with_name("harvey.local.yaml")
    shutil.copy(files.config, local)
    files.local = local
    files.template = files.config.read_bytes()
    return files


def test_after_harvey_train_setup_saves_to_harvey_local_yaml(trained, monkeypatch):
    script(monkeypatch, yes_no=[True, False], answers=["GB", "", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    assert load_config(str(trained.local)).compliance.precleared.enabled is True
    assert trained.config.read_bytes() == trained.template  # template untouched


def test_rerunning_setup_after_harvey_train_offers_the_saved_answers(trained, monkeypatch):
    set_precleared(trained.local, enabled=True, sender_country="US")
    asked = script(monkeypatch, yes_no=[None, False], answers=["", "", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()

    assert asked.yes_no[0][1] is True
    assert asked.questions[0].default == "US"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_local", [False, True])
async def test_setup_trains_into_the_file_harvey_reads(files, monkeypatch, has_local):
    if has_local:
        shutil.copy(files.config, files.config.with_name("harvey.local.yaml"))
    outputs = []

    class FakeTrainer:
        def __init__(self, **kwargs):
            pass

        async def train(self, url, output_path):
            outputs.append(output_path)
            return {"persona": {}}

    monkeypatch.setattr("harvey.trainer.Trainer", FakeTrainer)
    script(monkeypatch, yes_no=[], answers=["url", "https://example.test"])
    await setup.SetupWizard()._setup_product()

    name = "harvey.local.yaml" if has_local else "harvey.yaml"
    assert outputs == [str(files.config.with_name(name))]


async def summary(wizard, monkeypatch, capsys) -> str:
    monkeypatch.setattr(setup, "_check_claude_cli", lambda: True)
    capsys.readouterr()
    await wizard._print_summary()
    return capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_on", [True, False])
async def test_summary_shows_whether_the_check_was_saved_on(files, monkeypatch, capsys, turn_on):
    script(monkeypatch, yes_no=[turn_on, False], answers=["GB", "", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    finish(wizard)

    out = await summary(wizard, monkeypatch, capsys)
    assert ("✓ Cold-email check (Precleared)" in out) is turn_on
    assert ("– Cold-email check (Precleared)" in out) is not turn_on


@pytest.mark.asyncio
async def test_a_config_that_cannot_be_saved_is_reported_not_ticked(files, monkeypatch, capsys):
    files.config.write_text("channels:\n")  # hand-edited: an empty channels section
    script(monkeypatch, yes_no=[True, False], answers=["GB", "", ""])
    wizard = setup.SetupWizard()
    wizard._setup_compliance()
    wizard.config = {"channels": {"email": {"max_daily_sends": 50}}}
    wizard.env_vars["LINKEDIN_EMAIL"] = ""
    wizard._write_env()
    wizard._write_config()  # must not crash setup

    assert "Couldn't save" in capsys.readouterr().out
    assert files.config.read_text() == "channels:\n"
    out = await summary(wizard, monkeypatch, capsys)
    assert "– Cold-email check (Precleared)" in out
