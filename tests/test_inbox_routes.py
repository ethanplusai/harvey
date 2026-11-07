"""The dashboard's Inboxes routes: what they show, and who may change what."""

import asyncio

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird import inbox_routes
from sellbird.dashboard_auth import load_or_create_token
from sellbird.mailboxes import MailboxStore
from sellbird.models.campaign import Campaign, EmailStep

from tests.test_mailboxes import add_inbox, boxes  # noqa: F401  (boxes is a fixture)
from tests.test_outbox_native import Cfg, Env

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def env(tmp_path, monkeypatch, boxes):  # noqa: F811
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sellbird.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    agent = load_or_create_token(tmp_path / "data" / "agent-token")
    dash.agent_token_store.configure(tmp_path / "data" / "agent-token")
    monkeypatch.setattr(inbox_routes, "_config_env", lambda: (Cfg(), Env()))
    store = MailboxStore(str(db))

    async def setup():
        await store.init_db()
        mid = await add_inbox(store, "two@side.co", credential_ref="mailbox:x:smtp",
                              settings={"host": "smtp.side.co", "username": "two@side.co"})
        cid = await store.add_campaign(Campaign(
            id="", name="c", channel="email", prospect_ids=[], status="draft",
            sequence=[EmailStep(step=1, subject="s", body="b", delay_days=0)]))
        return mid, cid

    mid, cid = asyncio.run(setup())
    yield {"store": store, "mid": mid, "cid": cid, "user": dash.token_store.get(), "agent": agent}
    dash.agent_token_store.configure(dash.AGENT_TOKEN_FILE)


def _client(token):
    return TestClient(dash.app, base_url=ORIGIN, headers={"Authorization": f"Bearer {token}"})


def test_the_list_shows_every_inbox_and_no_secret(env):
    with _client(env["user"]) as c:
        data = c.get("/api/inboxes").json()
    text = str(data)
    assert "mailbox:x:smtp" not in text and "smtp.side.co" not in text
    by_address = {i["address"]: i for i in data["inboxes"]}
    side = by_address["two@side.co"]
    assert side["state"] == "sending" and side["cold_cap"] == 30 and not side["main"]
    assert any(i["main"] and i["id"] == "main" for i in data["inboxes"])
    assert data["several"] is True


def test_the_agent_can_read_and_pause_but_not_resume(env):
    with _client(env["agent"]) as c:
        assert c.get("/api/inboxes").status_code == 200
        r = c.post(f"/api/inboxes/{env['mid']}/pause", json={"reason": "bounces rising"})
        assert r.status_code == 200
        assert c.post(f"/api/inboxes/{env['mid']}/resume").status_code == 403
        assert c.post(f"/api/inboxes/{env['mid']}/cap", json={"cap": 99}).status_code == 403
    row = asyncio.run(env["store"].get_mailbox(env["mid"]))
    assert row["paused_reason"] == "bounces rising" and row["cap_override"] is None


def test_the_user_resumes_caps_and_names(env):
    mid = env["mid"]
    with _client(env["user"]) as c:
        assert c.post(f"/api/inboxes/{mid}/pause", json={}).json()["success"]
        assert c.post(f"/api/inboxes/{mid}/resume").json()["success"]
        assert c.post(f"/api/inboxes/{mid}/cap", json={"cap": 0}).status_code == 400
        assert c.post(f"/api/inboxes/{mid}/cap", json={"cap": "12"}).status_code == 400
        assert c.post(f"/api/inboxes/{mid}/cap", json={"cap": 12}).json()["success"]
        assert c.post(f"/api/inboxes/{mid}/name", json={"name": "A\nB"}).status_code == 400
        assert c.post(f"/api/inboxes/{mid}/name", json={"name": "Ethan R"}).json()["success"]
        assert c.post("/api/inboxes/main/pause").status_code == 404
    row = asyncio.run(env["store"].get_mailbox(mid))
    assert row["paused_reason"] == "" and row["cap_override"] == 12
    assert row["display_name"] == "Ethan R"


def test_removing_needs_the_address_typed(env):
    mid = env["mid"]
    with _client(env["user"]) as c:
        assert c.post(f"/api/inboxes/{mid}/remove", json={"confirm": "yes"}).status_code == 400
        assert c.post(f"/api/inboxes/{mid}/remove", json={"confirm": "two@side.co"}).json()["success"]
        assert c.post(f"/api/inboxes/{mid}/pause").status_code == 404
    assert asyncio.run(env["store"].get_mailbox(mid))["state"] == "retired"


def test_a_campaign_picks_its_inboxes(env):
    with _client(env["user"]) as c:
        path = f"/api/campaigns/{env['cid']}/inboxes"
        assert c.post(path, json={"inboxes": ["nope"]}).status_code == 404
        assert c.post(path, json={"inboxes": "main"}).status_code == 400
        assert c.post(path, json={"inboxes": ["main", env["mid"]]}).json()["success"]
        pools = c.get("/api/inboxes").json()["pools"]
    assert pools[env["cid"]] == sorted(["main", env["mid"]])
    assert asyncio.run(env["store"].campaign_pool(env["cid"])) == {"", env["mid"]}


def test_the_mcp_tools_list_and_pause_with_the_agents_token(env, tmp_path):
    import json
    import httpx
    from sellbird import dashboard_instance, mcp_server

    data = tmp_path / "data"
    dashboard_instance.write(data, host="127.0.0.1", port=5555, instance=dash.INSTANCE_ID)
    client = mcp_server.CoreClient(data, transport=httpx.ASGITransport(app=dash.app))
    server = mcp_server.build_server(lambda: client)

    async def call(name, args):
        return json.loads((await server.call_tool(name, args)).content[0].text)

    listed = asyncio.run(call("inboxes_list", {}))
    assert "two@side.co" in {i["address"] for i in listed["inboxes"]}
    paused = asyncio.run(call("inbox_pause", {"inbox_id": env["mid"], "reason": "agent saw bounces"}))
    assert paused["paused"] is True
    assert asyncio.run(env["store"].get_mailbox(env["mid"]))["paused_reason"] == "agent saw bounces"


# ── Connecting from the dashboard ──

@pytest.fixture
def keychain(monkeypatch):
    from sellbird.secrets import MemorySecretBackend, SecretStore
    backend = MemorySecretBackend()
    store = SecretStore(backend=backend)
    monkeypatch.setattr(inbox_routes, "_secrets", lambda: store)

    async def fake_test(row, config, env, secrets):
        return True, "Signed in."
    monkeypatch.setattr(inbox_routes, "_test", fake_test)
    return backend


SMTP = {"address": "Three@Side.co", "name": "Ethan", "smtp_host": "smtp.side.co",
        "password": "hunter2-secret"}


def test_the_user_connects_an_smtp_inbox_and_no_password_comes_back(env, keychain):
    with _client(env["user"]) as c:
        r = c.post("/api/inboxes/connect/smtp", json=SMTP)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] and body["address"] == "three@side.co" and body["test_ok"]
        listed = c.get("/api/inboxes").text
    assert "hunter2-secret" not in r.text and "hunter2-secret" not in listed
    assert "three@side.co" in listed
    row = asyncio.run(env["store"].get_mailbox_by_address("three@side.co"))
    assert row["connection"] == "smtp" and row["display_name"] == "Ethan"


def test_smtp_connect_refuses_bad_input(env, keychain):
    with _client(env["user"]) as c:
        assert c.post("/api/inboxes/connect/smtp", json={**SMTP, "password": ""}).status_code == 400
        assert c.post("/api/inboxes/connect/smtp", json={**SMTP, "smtp_host": ""}).status_code == 400
        assert c.post("/api/inboxes/connect/smtp", json={**SMTP, "address": "nope"}).status_code == 400
        assert c.post("/api/inboxes/connect/smtp", json={**SMTP, "smtp_port": 0}).status_code == 400
        assert c.post("/api/inboxes/connect/smtp",
                      json={**SMTP, "address": "two@side.co"}).status_code == 400  # already there
        assert c.post("/api/inboxes/connect/fax", json=SMTP).status_code == 404


def test_the_agent_can_never_connect_or_test_an_inbox(env, keychain):
    with _client(env["agent"]) as c:
        assert c.post("/api/inboxes/connect/smtp", json=SMTP).status_code == 403
        assert c.post("/api/inboxes/connect/gmail", json={"address": "a@b.co"}).status_code == 403
        assert c.get("/api/inboxes/connect/status").status_code == 403
        assert c.post(f"/api/inboxes/{env['mid']}/test").status_code == 403
        assert c.post(f"/api/inboxes/{env['mid']}/remove",
                      json={"confirm": "two@side.co"}).status_code == 403
    assert asyncio.run(env["store"].get_mailbox_by_address("three@side.co")) is None


def test_gmail_needs_its_client_first(env, keychain):
    with _client(env["user"]) as c:
        r = c.post("/api/inboxes/connect/gmail", json={"address": "g@side.co"})
    assert r.status_code == 400 and "GMAIL_CLIENT_ID" in r.json()["message"]


def test_a_gmail_sign_in_runs_in_the_background_and_connects(env, keychain, monkeypatch):
    class GEnv(Env):
        gmail_client_id = "id"
        gmail_client_secret = "secret"
    monkeypatch.setattr(inbox_routes, "_config_env", lambda: (Cfg(), GEnv()))
    seen = {}

    def fake_sign_in(kind, address, env_):
        seen["args"] = (kind, address)
        return {"refresh_token": "rt-secret", "scope": "x", "obtained_at": 1}
    monkeypatch.setattr(inbox_routes, "_sign_in", fake_sign_in)
    with _client(env["user"]) as c:
        r = c.post("/api/inboxes/connect/gmail", json={"address": "G@Side.co", "name": "E"})
        assert r.json()["started"]
        for _ in range(100):
            status = c.get("/api/inboxes/connect/status").json()
            if status["state"] != "waiting":
                break
            import time
            time.sleep(0.02)
        listed = c.get("/api/inboxes").text
    assert status["state"] == "connected", status
    assert seen["args"] == ("gmail", "g@side.co")
    assert "rt-secret" not in listed and "g@side.co" in listed


def test_main_mailbox_not_connected_says_how(env):
    with _client(env["user"]) as c:
        data = c.get("/api/inboxes").json()
    main = next(i for i in data["inboxes"] if i["main"])
    if main["state"] == "not_connected":
        assert main["connect_hint"]
