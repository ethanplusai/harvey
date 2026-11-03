"""Settings → Email finders: keys are saved to .env like the verifier keys,
shown back as "set" only, and the finder order is checked."""

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.config import load_env

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    for var in ("PROSPEO_API_KEY", "APOLLO_API_KEY", "EMAIL_FINDERS"):
        monkeypatch.delenv(var, raising=False)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        c.env_file = tmp_path / ".env"
        yield c


def test_finder_keys_save_to_env_and_show_back_as_set(client):
    before = client.get("/api/settings").json()
    assert before["prospeo_api_key_set"] is False and before["email_finders"] == ""

    resp = client.post("/api/settings/env", json={
        "EMAIL_FINDERS": " Prospeo, apollo ,prospeo", "PROSPEO_API_KEY": "pk-test",
        "APOLLO_API_KEY": "ak-test"})
    assert resp.json()["success"] is True
    text = client.env_file.read_text()
    assert "EMAIL_FINDERS=prospeo,apollo" in text
    assert "PROSPEO_API_KEY=pk-test" in text and "APOLLO_API_KEY=ak-test" in text

    after = client.get("/api/settings").json()
    assert after["prospeo_api_key_set"] and after["apollo_api_key_set"]
    assert after["email_finders"] == "prospeo,apollo"
    assert "pk-test" not in str(after) and "ak-test" not in str(after)


def test_unknown_finder_is_refused(client):
    resp = client.post("/api/settings/env", json={"EMAIL_FINDERS": "prospeo,linkedin"})
    assert resp.status_code == 400 and "linkedin" in resp.json()["message"]
    assert not client.env_file.exists()


def test_load_env_reads_the_finder_settings(monkeypatch):
    monkeypatch.setenv("PROSPEO_API_KEY", " pk ")
    monkeypatch.setenv("APOLLO_API_KEY", "ak")
    monkeypatch.setenv("EMAIL_FINDERS", "apollo")
    env = load_env()
    assert (env.prospeo_api_key, env.apollo_api_key, env.email_finders) == ("pk", "ak", "apollo")


def test_saved_keys_show_only_a_masked_hint(client):
    client.post("/api/settings/env", json={
        "REOON_API_KEY": "reoon-key-0000-abcd", "APOLLO_API_KEY": "apollo-key-0000-wxyz",
        "PROSPEO_API_KEY": "short"})
    data = client.get("/api/settings").json()
    assert data["reoon_api_key_hint"] == "••••abcd"
    assert data["apollo_api_key_hint"] == "••••wxyz"
    assert data["prospeo_api_key_hint"] == "••••"        # too short to show any of it
    assert data["zerobounce_api_key_hint"] == ""         # not set
    body = str(data)
    for secret in ("reoon-key-0000", "apollo-key-0000", "short"):
        assert secret not in body


def test_settings_page_puts_finders_in_the_verification_card(client):
    html = client.get("/").text
    card = html[html.index("Email verification"):html.index('<div class="subhead">Optional</div>')]
    assert "<h3>Verifiers</h3>" in card and "<h3>Email finders" in card
    for field in ("reoon-key", "zerobounce-key", "hunter-key", "prospeo-key", "apollo-key"):
        assert f"toggleVisibility('{field}')" in card
