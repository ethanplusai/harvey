"""The MCP server and the agent's token: what the coding agent can and can't reach."""

import json

import httpx
import pytest
import pytest_asyncio

import sellbird.dashboard as dash
from sellbird import agent_policy, dashboard_instance, extensions, mcp_server
from sellbird.dashboard_auth import load_or_create_token
from sellbird.state import StateManager

ORIGIN = "http://127.0.0.1:5555"


class Recorder(httpx.AsyncBaseTransport):
    """Drives the real dashboard app and remembers each request."""

    def __init__(self):
        self.inner = httpx.ASGITransport(app=dash.app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "sellbird.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr("sellbird.state.DB_PATH", db)
    await StateManager(str(db)).init_db()
    token = load_or_create_token(data / "agent-token")
    dash.agent_token_store.configure(data / "agent-token")
    assert dash.agent_token_store.get() == token
    dashboard_instance.write(data, host="127.0.0.1", port=5555, instance=dash.INSTANCE_ID)
    transport = Recorder()
    yield mcp_server.CoreClient(data, transport=transport), transport, data
    dash.agent_token_store.configure(dash.AGENT_TOKEN_FILE)


def _server(client):
    return mcp_server.build_server(lambda: client)


async def _call(server, name, args=None):
    blocks = await server.call_tool(name, args or {})
    return json.loads(blocks.content[0].text)


# ── reading ──

@pytest.mark.asyncio
async def test_reads_go_out_with_the_agent_token_only_after_the_instance_matches(core):
    client, transport, data = core
    out = await _call(_server(client), "stats")
    assert out.get("ok") is not False
    first, second = transport.requests[0], transport.requests[1]
    assert first.url.path == "/api/instance" and "authorization" not in first.headers
    agent = (data / "agent-token").read_text().strip()
    assert second.headers["authorization"] == f"Bearer {agent}"


@pytest.mark.parametrize("tool", ["today", "setup_status", "stats", "activity", "usage",
                                  "campaigns_list", "prospects_list", "companies_list",
                                  "conversations_list", "outbox_list", "signals_list",
                                  "discover_providers", "sending_status"])
@pytest.mark.asyncio
async def test_every_read_tool_is_allowed(core, tool):
    client, _, _ = core
    out = await _call(_server(client), tool)
    assert out.get("error") != "user_only", out


@pytest.mark.asyncio
async def test_a_different_program_on_the_port_never_gets_the_token(core):
    client, transport, data = core
    dashboard_instance.write(data, host="127.0.0.1", port=5555, instance="someone-else")
    out = await _call(_server(client), "stats")
    assert out == {"ok": False, "message": mcp_server.NOT_RUNNING}
    assert [r.url.path for r in transport.requests] == ["/api/instance"]


@pytest.mark.asyncio
async def test_no_dashboard_file_means_not_running(core):
    client, transport, data = core
    (data / dashboard_instance.INFO_NAME).unlink()
    out = await _call(_server(client), "today")
    assert out["ok"] is False and "sellbird dashboard" in out["message"]
    assert transport.requests == []


# ── pausing, and what stays the user's ──

@pytest.mark.asyncio
async def test_the_agent_can_pause_but_never_resume(core):
    client, _, _ = core
    server = _server(client)
    paused = await _call(server, "sending_pause")
    assert paused["paused"] is True
    resumed = await client.post("/api/sending/resume")
    assert resumed["ok"] is False and resumed["status"] == 403
    assert resumed["error"] == "user_only"


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/outbox/x/approve"), ("POST", "/api/outbox/approve-all"),
    ("POST", "/api/settings/env"), ("POST", "/api/signals/status"),
    ("POST", "/api/discover/run"), ("POST", "/api/sending-mode"),
    ("POST", "/api/trulyinbox/connect"), ("GET", "/api/settings"),
    ("GET", "/api/sellbird/logs"), ("GET", "/api/export/prospects.csv"),
    ("POST", "/api/prospects/p1/opt-out/undo"), ("POST", "/api/sellbird/start"),
    ("POST", "/api/inboxes/x/resume"), ("POST", "/api/inboxes/x/cap"),
    ("POST", "/api/inboxes/x/remove"), ("POST", "/api/inboxes/x/name"),
    ("POST", "/api/campaigns/c/inboxes"),
])
@pytest.mark.asyncio
async def test_everything_else_is_the_users(core, method, path):
    client, _, _ = core
    out = await client._request(method, path, json={} if method == "POST" else None)
    assert out["status"] == 403 and out["error"] == "user_only"


def test_the_users_token_still_reaches_everything(core):
    from fastapi.testclient import TestClient
    token = dash.token_store.get()
    with TestClient(dash.app, base_url=ORIGIN) as http:
        response = http.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_the_allowlist_is_reads_estimates_and_pause_only():
    for method, pattern in agent_policy.AGENT_ROUTES:
        assert method == "GET" or pattern in ("/api/discover/estimate", "/api/sending/pause",
                                              "/api/inboxes/[^/]+/pause")
    assert agent_policy.agent_may("HEAD", "/api/today")
    assert not agent_policy.agent_may("GET", "/api/today/extra")
    assert not agent_policy.agent_may("POST", "/api/sending/resume")


def test_an_extension_can_open_one_route_to_the_agent(monkeypatch):
    monkeypatch.setattr(extensions, "_REGISTRY", extensions.Registry())
    monkeypatch.setattr(extensions, "_LOADED", True)
    extensions._REGISTRY.add_agent_route("GET", r"/api/ext/[a-z]+")
    extra = agent_policy._compile(extensions._REGISTRY.agent_routes)
    assert agent_policy.agent_may("GET", "/api/ext/stats", extra)
    assert not agent_policy.agent_may("POST", "/api/ext/stats", extra)
    with pytest.raises(extensions.ExtensionError):
        extensions._REGISTRY.add_agent_route("FETCH", "/x")


@pytest.mark.asyncio
async def test_an_extension_adds_mcp_tools(core, monkeypatch):
    client, _, _ = core
    monkeypatch.setattr(extensions, "_REGISTRY", extensions.Registry())
    monkeypatch.setattr(extensions, "_LOADED", True)

    def register(server, client_factory, guarded):
        @server.tool()
        async def ext_hello() -> dict:
            """Say hello."""
            return await guarded(lambda c: c.get("/api/stats"))

    extensions._REGISTRY.add_mcp_tools("ext.hello", register)
    server = _server(client)
    names = {t.name for t in await server.list_tools()}
    assert "ext_hello" in names and "today" in names


# ── the dashboard file ──

def test_the_dashboard_file_round_trips_and_is_removed_only_by_its_owner(tmp_path):
    dashboard_instance.write(tmp_path, host="127.0.0.1", port=5555, instance="a")
    info = dashboard_instance.read(tmp_path)
    assert (info.host, info.port, info.instance) == ("127.0.0.1", 5555, "a")
    dashboard_instance.remove(tmp_path, instance="b")       # a newer dashboard's file stays
    assert dashboard_instance.read(tmp_path) is not None
    dashboard_instance.remove(tmp_path, instance="a")
    assert dashboard_instance.read(tmp_path) is None


@pytest.mark.parametrize("text", ["", "{", "[]", '{"host":"h","port":0,"instance":"a","pid":1}',
                                  '{"host":"h","port":"x","instance":"a","pid":1}'])
def test_a_bad_dashboard_file_is_no_dashboard(tmp_path, text):
    (tmp_path / dashboard_instance.INFO_NAME).write_text(text)
    assert dashboard_instance.read(tmp_path) is None


def test_instance_needs_no_token():
    from fastapi.testclient import TestClient
    with TestClient(dash.app, base_url=ORIGIN) as http:
        assert http.get("/api/instance").json() == {"instance": dash.INSTANCE_ID}
