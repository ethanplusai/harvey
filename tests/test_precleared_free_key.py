"""Tests for getting a free Precleared key in one step: create_free_key, the
`harvey precleared-key` command and the hold message that points to it. No
network: Precleared is an httpx.MockTransport or a stand-in function."""

import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest

import harvey.config as config_mod
from harvey import cli, dashboard, setup
from harvey.integrations import precleared
from harvey.integrations.precleared import KEYS_URL, NewKey, create_free_key

FAKE_KEY = "pc_live_example_not_a_real_key"
MADE = {"key": FAKE_KEY, "key_prefix": FAKE_KEY[:12], "free_per_day": 25,
        "message": "Save it now. We only store a hash."}


def answer(status=201, body=MADE, seen=None, error=None):
    """A Precleared stand-in that records each request and gives one answer."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        if error is not None:
            raise error
        content = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(status, content=content,
                              headers={"Content-Type": "application/json"})

    return httpx.MockTransport(handler)


# ── create_free_key ──


def test_a_free_key_is_asked_for_with_only_the_address_country_and_use():
    seen = []
    result = create_free_key(" jo@example.com ", country="gb", transport=answer(seen=seen))

    assert result == NewKey(key=FAKE_KEY)
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", KEYS_URL)
    assert json.loads(request.content) == {
        "email": "jo@example.com", "use_case": precleared.KEY_USE_CASE, "country": "GB"}
    assert request.headers["User-Agent"] == precleared.USER_AGENT
    assert "authorization" not in request.headers


def test_a_country_precleared_does_not_take_is_left_out():
    seen = []
    create_free_key("jo@example.com", country="DE", transport=answer(seen=seen))
    assert "country" not in json.loads(seen[0].content)


@pytest.mark.parametrize("email", ["", "  ", "jo", "jo@example", "jo @example.com",
                                   "jo@@example.com", "a" * 250 + "@example.com"])
def test_a_bad_address_is_refused_without_asking_precleared(email):
    seen = []
    result = create_free_key(email, transport=answer(seen=seen))
    assert (result.key, result.bad_email) == ("", True)
    assert seen == []


@pytest.mark.parametrize("status,body,error,bad_email", [
    (429, {"error": "too_many_keys", "message": "This email address already has three keys."},
     "This email address already has three keys", False),
    (429, {"error": "too_many_keys",
           "message": "Too many keys from this network today. Try again tomorrow."},
     "Too many keys from this network today. Try again tomorrow", False),
    (400, {"error": "invalid_request", "message": "Give a valid email address."},
     "Give a valid email address", True),
    (500, {"error": "server_error", "message": "The key could not be created."},
     "The key could not be created", False),
    (502, "<html>Bad gateway</html>", "Precleared answered HTTP 502", False),
])
def test_when_no_key_comes_back_precleared_s_reason_does(status, body, error, bad_email):
    result = create_free_key("jo@example.com", transport=answer(status, body))
    assert result == NewKey(error=error, bad_email=bad_email)


def test_a_reason_cannot_drive_the_terminal():
    body = {"error": "x", "message": "No\x1b]8;;https://bad.example\x07 key\x1b[2J today."}
    result = create_free_key("jo@example.com", transport=answer(429, body))
    assert not any(ch in result.error for ch in "\x1b\x07")
    assert result.error.startswith("No")


@pytest.mark.parametrize("body", [{}, {"key": ""}, {"key": "has a space in it 12345"},
                                  {"key": 12345}, {"key": "short"}, ["not", "an", "object"]])
def test_an_answer_without_a_usable_key_saves_nothing(body):
    result = create_free_key("jo@example.com", transport=answer(201, body))
    assert result.key == ""
    assert result.error == "Precleared's answer had no key in it"


def test_no_answer_is_said_plainly():
    result = create_free_key("jo@example.com",
                             transport=answer(error=httpx.ConnectError("no route")))
    assert result == NewKey(error="Precleared couldn't be reached")


# ── harvey precleared-key ──


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """An empty .env in a temp folder; the real .env and config are never read."""
    env = tmp_path / ".env"
    monkeypatch.setattr(setup, "ENV_FILE", env)
    monkeypatch.setattr(dashboard, "ENV_FILE", env)
    # setenv first so the key the command loads is removed again afterwards.
    monkeypatch.setenv("PRECLEARED_API_KEY", "")
    monkeypatch.delenv("PRECLEARED_API_KEY")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(dashboard, "load_dotenv", lambda *a, **k: False)
    configured(monkeypatch, enabled=True, country="GB")
    return env


def configured(monkeypatch, enabled, country):
    settings = SimpleNamespace(enabled=enabled, sender_country=country)
    monkeypatch.setattr(config_mod, "load_config",
                        lambda *a, **k: SimpleNamespace(compliance=SimpleNamespace(precleared=settings)))


def precleared_answers(monkeypatch, result):
    calls = []

    def create(email, country=""):
        calls.append((email, country))
        return result

    monkeypatch.setattr(precleared, "create_free_key", create)
    return calls


def run(email=""):
    with pytest.raises(SystemExit) as exit_:
        cli.cmd_precleared_key(SimpleNamespace(email=email))
    return exit_.value.code


def test_the_command_saves_a_free_key_and_never_prints_it(env_file, monkeypatch, capsys):
    env_file.write_text("SMTP_PASSWORD=a=b=c\n# a comment\n")
    calls = precleared_answers(monkeypatch, NewKey(key=FAKE_KEY))

    assert run("jo@example.com") == 0

    lines = env_file.read_text().splitlines()
    assert f"PRECLEARED_API_KEY={FAKE_KEY}" in lines
    assert "SMTP_PASSWORD=a=b=c" in lines and "# a comment" in lines
    assert calls == [("jo@example.com", "GB")]
    out = capsys.readouterr().out
    assert "saved in .env as PRECLEARED_API_KEY" in out
    assert "100 checks a day for 14 days" in out
    assert FAKE_KEY not in out
    if os.name != "nt":   # Windows uses the folder's ACLs, not POSIX modes
        assert env_file.stat().st_mode & 0o777 == 0o600


def test_without_email_the_command_asks_for_one(env_file, monkeypatch, capsys):
    calls = precleared_answers(monkeypatch, NewKey(key=FAKE_KEY))
    monkeypatch.setattr("builtins.input", lambda prompt="": "jo@example.com")

    assert run() == 0

    assert calls == [("jo@example.com", "GB")]
    assert "precleared.dev/privacy" in capsys.readouterr().out


def test_a_blank_answer_gets_no_key(env_file, monkeypatch, capsys):
    precleared_answers(monkeypatch, NewKey(key=FAKE_KEY))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert run() == 1
    assert not env_file.exists()


@pytest.mark.parametrize("where", ["file", "environment"])
def test_the_command_keeps_a_key_that_is_already_there(env_file, monkeypatch, capsys, where):
    if where == "file":
        env_file.write_text("PRECLEARED_API_KEY=existing_key\n")
    else:
        monkeypatch.setenv("PRECLEARED_API_KEY", "existing_key")
    calls = precleared_answers(monkeypatch, NewKey(key=FAKE_KEY))

    assert run("jo@example.com") == 0

    assert calls == []
    assert "kept it" in capsys.readouterr().out
    assert where == "environment" or env_file.read_text() == "PRECLEARED_API_KEY=existing_key\n"


def test_the_command_says_why_no_key_came_back(env_file, monkeypatch, capsys):
    precleared_answers(monkeypatch, NewKey(error="Too many keys from this network today"))

    assert run("jo@example.com") == 1

    out = capsys.readouterr().out
    assert "Couldn't get a key: Too many keys from this network today." in out
    assert "https://precleared.dev/get-key" in out
    assert not env_file.exists()


def test_with_the_check_off_the_key_is_saved_and_the_user_told(env_file, monkeypatch, capsys):
    configured(monkeypatch, enabled=False, country="GB")
    calls = precleared_answers(monkeypatch, NewKey(key=FAKE_KEY))

    assert run("jo@example.com") == 0

    assert calls == [("jo@example.com", "")]  # the sender country goes only when the check is on
    assert "The check itself is off" in capsys.readouterr().out
    assert f"PRECLEARED_API_KEY={FAKE_KEY}" in env_file.read_text().splitlines()


def test_harvey_precleared_key_is_a_command(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_precleared_key", lambda args: seen.append(args.email))
    monkeypatch.setattr(sys, "argv", ["harvey", "precleared-key", "--email", "jo@example.com"])

    cli.main()

    assert seen == ["jo@example.com"]


# ── the hold points to the command ──


def test_the_keyless_hold_points_to_the_command():
    message = precleared.hold_message({"kind": "keyless_limit", "waiting": 3})

    assert "harvey precleared-key" in message["detail"]
    assert "100 a day for the first 14 days, then 25" in message["detail"]
    assert "PRECLEARED_API_KEY" in message["detail"]
    assert message["url"] == precleared.GET_KEY_URL


@pytest.mark.asyncio
async def test_the_keyless_limit_reason_names_the_command():
    client = precleared.PreclearedClient("GB", transport=answer(429, {"error": "daily_limit"}))

    result = await client.clear("person@example.com", SimpleNamespace(
        get_setting=_none, set_setting=_none))

    assert result.held and result.hold_kind == "keyless_limit"
    assert "`harvey precleared-key`" in result.reason
    assert "precleared.dev/get-key" in result.reason


async def _none(*args, **kwargs):
    return None
