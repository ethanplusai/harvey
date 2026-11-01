"""Retraining replaces what training learns and keeps every other setting.

`harvey train` used to rewrite the config from scratch, so retraining reset
the email provider to instantly, quiet hours to New York and turned the
Precleared check off. These tests pin down what training owns.
"""

import yaml

from harvey.config import HarveyConfig
from harvey.paths import PROJECT_ROOT
from harvey.textio import read_user_text
from harvey.trainer import write_trained_config

LEARNED = {
    "persona": {
        "name": "Harvey", "company": "Acme Roofing", "role": "Business Development",
        "email": "harvey@acme.com", "linkedin": "linkedin.com/in/harvey-acme-com",
        "tone": "warm, direct",
    },
    "product": {
        "name": "RoofBot", "description": "Answers roofing calls",
        "pricing": "$99/mo", "key_benefits": ["Never miss a call"],
        "objection_responses": {"too expensive": "One job pays for a year."},
    },
    "icp": {
        "industries": ["Roofing"], "company_size": "1-20 employees",
        "titles": ["Owner"], "geography": ["United States"],
    },
    "channels": {
        "email": {"enabled": True, "provider": "instantly", "max_daily_sends": 50},
        "linkedin": {"enabled": True, "max_daily_connections": 20, "max_daily_messages": 10},
    },
    "usage": {
        "max_daily_claude_percent": 80, "heartbeat_interval_minutes": 15,
        "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "America/New_York"},
    },
    "brain": {"provider": "claude"},
}

# A config someone trained before and then tuned by hand.
TUNED = {
    "persona": {
        "name": "Ethan", "company": "Old Co", "role": "Founder",
        "email": "ethan@oldco.com", "linkedin": "linkedin.com/in/ethan", "tone": "casual",
    },
    "product": {
        "name": "OldBot", "description": "Old", "pricing": "$1",
        "key_benefits": ["old"], "objection_responses": {},
        "offer": {"goal": "book_call", "booking_url": "https://cal.com/ethan"},
    },
    "icp": {
        "industries": ["SaaS"], "company_size": "10-50", "titles": ["CEO"],
        "geography": ["UK"], "hiring_signals": ["SDR"],
        "geo_coordinates": {"London": "51.5,-0.12,50"},
    },
    "channels": {
        "email": {
            "enabled": True, "provider": "gmail", "max_daily_sends": 20,
            "require_approval": False, "send_to_risky": True, "max_bounce_rate": 0.02,
        },
        "linkedin": {"enabled": False, "max_daily_connections": 5, "max_daily_messages": 2},
    },
    "usage": {
        "max_daily_claude_percent": 40, "heartbeat_interval_minutes": 30,
        "quiet_hours": {"start": "20:00", "end": "08:00", "timezone": "Europe/London"},
    },
    "brain": {"provider": "claude"},
    "compliance": {"precleared": {
        "enabled": True, "sender_country": "GB", "address_source": "published",
        "prospect_country": "GB",
    }},
    "writer": {"humanizer": True},
}


def write(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def read(path):
    return yaml.safe_load(path.read_text())


def test_retraining_keeps_every_setting_training_does_not_learn(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    write(path, TUNED)
    out = write_trained_config(LEARNED, path)

    assert read(path) == out
    # Settings the user chose survive.
    assert out["channels"] == TUNED["channels"]
    assert out["usage"] == TUNED["usage"]
    assert out["compliance"] == TUNED["compliance"]
    assert out["writer"] == TUNED["writer"]
    assert out["product"]["offer"] == TUNED["product"]["offer"]
    assert out["icp"]["hiring_signals"] == ["SDR"]
    assert out["icp"]["geo_coordinates"] == {"London": "51.5,-0.12,50"}
    # The sender identity and voice are the user's, not training guesses.
    for key in ("name", "role", "email", "linkedin", "tone"):
        assert out["persona"][key] == TUNED["persona"][key]
    # What training learns is replaced.
    assert out["persona"]["company"] == "Acme Roofing"
    for key in ("name", "description", "pricing", "key_benefits", "objection_responses"):
        assert out["product"][key] == LEARNED["product"][key]
    for key in ("industries", "company_size", "titles", "geography"):
        assert out["icp"][key] == LEARNED["icp"][key]
    HarveyConfig(**out)  # still a valid config


def test_brain_selection_comes_from_the_trainer(tmp_path):
    # `harvey train --provider codex` must win over the file's old choice.
    path = tmp_path / "harvey.local.yaml"
    write(path, TUNED)
    learned = {**LEARNED, "brain": {"provider": "codex", "model": "gpt-5.5"}}
    assert write_trained_config(learned, path)["brain"] == {"provider": "codex", "model": "gpt-5.5"}


def test_first_training_keeps_the_template_settings(tmp_path):
    # No harvey.local.yaml yet: the settings in harvey.yaml (gmail, the
    # Precleared block) carry over, and the untrained placeholder persona
    # is replaced with what training learned.
    template = tmp_path / "harvey.yaml"
    # Read and written the way Harvey itself does it: the shipped template
    # may carry a BOM, which a default-encoding write on Windows cannot
    # encode (cp1252).
    shipped = read_user_text(PROJECT_ROOT / "harvey.yaml")
    template.write_text(shipped, encoding="utf-8")
    path = tmp_path / "harvey.local.yaml"
    out = write_trained_config(LEARNED, path)

    assert out["channels"]["email"]["provider"] == "gmail"
    assert out["compliance"]["precleared"]["enabled"] is False
    assert out["persona"] == LEARNED["persona"]
    assert out["product"]["offer"]["goal"] == "book_call"
    assert read_user_text(template) == shipped  # the template itself is untouched
    HarveyConfig(**out)


def test_no_existing_config_writes_what_training_built(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    assert write_trained_config(LEARNED, path) == LEARNED
    assert read(path) == LEARNED


def test_unreadable_config_is_backed_up_not_lost(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    path.write_text("channels: [unclosed\n")
    out = write_trained_config(LEARNED, path)
    assert out == LEARNED
    backups = list(tmp_path.glob("harvey.local.yaml*.bak"))
    assert [b.read_text() for b in backups] == ["channels: [unclosed\n"]


def test_an_earlier_backup_is_never_overwritten(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    (tmp_path / "harvey.local.yaml.bak").write_text("precious")
    path.write_text("channels: [unclosed\n")
    write_trained_config(LEARNED, path)
    assert (tmp_path / "harvey.local.yaml.bak").read_text() == "precious"
    assert sorted(b.read_text() for b in tmp_path.glob("harvey.local.yaml*.bak")) == [
        "channels: [unclosed\n", "precious",
    ]


def test_an_empty_config_needs_no_backup(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    path.write_text("\n")
    assert write_trained_config(LEARNED, path) == LEARNED
    assert list(tmp_path.glob("*.bak")) == []


def test_first_training_ignores_configs_outside_harveys_folder(tmp_path, monkeypatch):
    # Running `harvey train` from another project's folder must not copy that
    # project's provider or sender identity into Harvey.
    elsewhere = tmp_path / "other-project"
    elsewhere.mkdir()
    write(elsewhere / "harvey.local.yaml", TUNED)
    monkeypatch.chdir(elsewhere)
    home = tmp_path / "harvey"
    home.mkdir()
    assert write_trained_config(LEARNED, home / "harvey.local.yaml") == LEARNED


def test_a_real_sender_with_a_blank_company_is_kept(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    write(path, {**TUNED, "persona": {**TUNED["persona"], "company": ""}})
    out = write_trained_config(LEARNED, path)
    assert out["persona"]["email"] == "ethan@oldco.com"
    assert out["persona"]["company"] == "Acme Roofing"


def test_a_failed_write_leaves_the_config_and_no_temp_file(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    write(path, TUNED)
    before = path.read_text()
    bad = {**LEARNED, "product": {**LEARNED["product"], "name": object()}}
    try:
        write_trained_config(bad, path)
    except yaml.YAMLError:
        pass
    else:
        raise AssertionError("an unrepresentable value should fail the write")
    assert path.read_text() == before
    assert list(tmp_path.glob("*.tmp")) == []
