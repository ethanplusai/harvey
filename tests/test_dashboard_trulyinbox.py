"""The dashboard's warmup setup step: get TrulyInbox, connect it, or say you
use another service. Either answer completes the step, so nobody is asked twice."""

import pytest

import sellbird.dashboard as dash
from sellbird.integrations import trulyinbox as ti
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)


def _step(client):
    checks = client.get("/api/setup-status").json()["checks"]
    return next(c for c in checks if c["id"] == "warmup_service")


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("TRULYINBOX_API_KEY", raising=False)
    monkeypatch.delenv("TRULYINBOX_WORKSPACE_ID", raising=False)


def test_the_step_starts_open_with_the_link(client):
    step = _step(client)
    assert not step["done"] and step["required"]
    assert step["link"] == ti.AFFILIATE_URL


def test_another_service_completes_the_step(client):
    assert client.post("/api/trulyinbox/other").json()["success"]
    step = _step(client)
    assert step["done"] and "your own" in step["label"]


def test_connecting_checks_the_key_then_saves_it(client, monkeypatch):
    seen = {}

    async def fake_health(self, address):
        seen["key"] = self._key
        self.workspace_id = "42"
        return ti.MailboxHealth(address=address, found=False)
    monkeypatch.setattr(ti.TrulyInbox, "mailbox_health", fake_health)
    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    r = client.post("/api/trulyinbox/connect", json={"key": "  ti_ws_abc  "}).json()
    assert r["success"] and "isn't in your TrulyInbox" in r["message"]
    assert seen["key"] == "ti_ws_abc"
    assert written == {"TRULYINBOX_API_KEY": "ti_ws_abc", "TRULYINBOX_WORKSPACE_ID": "42"}
    assert _step(client)["done"]


def test_a_rejected_key_is_not_saved(client, monkeypatch):
    async def reject(self, address):
        raise ti.TrulyInboxError(ti.KEY_REJECTED)
    monkeypatch.setattr(ti.TrulyInbox, "mailbox_health", reject)
    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    r = client.post("/api/trulyinbox/connect", json={"key": "ti_ws_bad"})
    assert r.status_code == 400 and "rejected" in r.json()["message"]
    assert not written and not _step(client)["done"]


@pytest.mark.parametrize("body", [{}, {"key": ""}, {"key": 5}, {"key": "x" * 600}, []])
def test_a_missing_key_is_refused(client, body):
    assert client.post("/api/trulyinbox/connect", json=body).status_code == 400


def test_connect_without_a_sending_address_is_refused(client, monkeypatch):
    monkeypatch.setattr(ti, "sending_address", lambda config: "")
    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    r = client.post("/api/trulyinbox/connect", json={"key": "ti_ws_abc"})
    assert r.status_code == 400 and "sends from" in r.json()["message"] and not written


def test_the_step_is_optional_on_instantly(client, monkeypatch):
    monkeypatch.setattr(dash, "_current_provider", lambda: "instantly")
    step = _step(client)
    assert not step["required"] and step["link"] == ti.AFFILIATE_URL
