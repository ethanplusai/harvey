"""The .env Sellbird reads is the home folder's, wherever that folder is.

SELLBIRD_HOME can point outside the checkout (one checkout, several
projects). A bare load_dotenv() searched upward from the package instead, so
the home folder's keys were silently ignored.
"""

import os
import subprocess
import sys
from pathlib import Path

import sellbird.config as config_mod

REPO = Path(__file__).resolve().parent.parent
KEYS = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD")


def _home_with_env(tmp_path):
    home = tmp_path / "project-home"
    home.mkdir()
    (home / ".env").write_text(
        "SMTP_HOST=smtp.example.test\n"
        "SMTP_USERNAME=sender@example.com\n"
        "SMTP_PASSWORD=fixture-password\n",
        encoding="utf-8",
    )
    return home


def _clear_keys(monkeypatch):
    # setenv first so whatever load_dotenv adds is removed again afterwards.
    for key in KEYS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def test_load_env_reads_the_home_folders_env(tmp_path, monkeypatch):
    home = _home_with_env(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", home)
    _clear_keys(monkeypatch)

    env = config_mod.load_env()

    assert env.smtp_host == "smtp.example.test"
    assert env.smtp_username == "sender@example.com"
    assert env.smtp_password == "fixture-password"


def test_load_env_keeps_values_already_in_the_environment(tmp_path, monkeypatch):
    home = _home_with_env(tmp_path)
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", home)
    _clear_keys(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.net")

    assert config_mod.load_env().smtp_host == "smtp.example.net"


def test_sellbird_home_outside_the_checkout_end_to_end(tmp_path):
    """The reported case: SELLBIRD_HOME set, run from another folder."""
    home = _home_with_env(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    env["SELLBIRD_HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))

    out = subprocess.run(
        [sys.executable, "-c",
         "from sellbird.config import load_env; print(load_env().smtp_host)"],
        cwd=elsewhere, env=env, capture_output=True, text=True, timeout=60,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "smtp.example.test"
