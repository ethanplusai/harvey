"""Compose must preserve atomic saves without exposing the dashboard."""
from pathlib import Path

import yaml

from sellbird.paths import PROJECT_ROOT


def service():
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())["services"]["sellbird"]


def test_compose_preserves_loopback_and_uses_dashboard():
    config = service()
    assert config["network_mode"] == "host"
    assert not config.get("ports")
    assert config["command"] == ["dashboard"]
    assert config["init"] is True
    # Credentials edited through setup/dashboard must not be masked by an
    # environment snapshot captured when the container was created.
    assert "env_file" not in config


def test_compose_mounts_directory_for_atomic_saves():
    mounts = service()["volumes"]
    root = next(mount for mount in mounts if mount["target"] == "/app")
    assert root["type"] == "bind" and root["source"] == "."
    assert root["bind"]["create_host_path"] is False
    assert not root.get("read_only", False)
    # Nested single-file mounts would prevent os.replace for these files.
    assert not any(mount["target"].startswith("/app/") for mount in mounts)


def test_setup_and_atomic_training_saves_survive_reloading(tmp_path, monkeypatch):
    from sellbird import setup, dashboard
    from sellbird.config import load_config
    from sellbird.trainer import write_trained_config

    base = tmp_path / "sellbird.yaml"
    base.write_text((PROJECT_ROOT / "sellbird.yaml").read_text())
    env = tmp_path / ".env"
    env.write_text("SERPER_API_KEY=fixture_original\n")
    monkeypatch.setattr(setup, "CONFIG_FILE", base)
    monkeypatch.setattr(setup, "ENV_FILE", env)
    monkeypatch.setattr(dashboard, "ENV_FILE", env)
    wizard = setup.SetupWizard()
    wizard.env_vars = {"SERPER_API_KEY": "fixture_setup"}
    wizard.config = {"usage": {"max_daily_claude_percent": 7}}
    wizard._write_env()
    wizard._write_config()
    assert wizard.config_saved
    dashboard._write_env_file({"SERPER_API_KEY": "fixture_dashboard"})
    learned = yaml.safe_load(base.read_text())
    learned["product"]["name"] = "Fixture Product"
    local = tmp_path / "sellbird.local.yaml"
    write_trained_config(learned, local)
    learned["product"]["name"] = "Fixture Updated"
    write_trained_config(learned, local)
    assert load_config(str(local)).product.name == "Fixture Updated"
    assert load_config(str(local)).usage.max_daily_claude_percent == 7
    assert "SERPER_API_KEY=fixture_dashboard" in env.read_text()
