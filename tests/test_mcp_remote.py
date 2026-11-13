"""`sellbird grok`: the local MCP tools over HTTP, only with the server's key."""

from starlette.testclient import TestClient

from sellbird import mcp_remote
from sellbird.mcp_server import build_server

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _client(key="k" * 43):
    return TestClient(mcp_remote.build_app(key, server=build_server()))


def test_no_key_no_wrong_key_nothing_runs():
    with _client() as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        wrong = c.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer nope"})
        assert wrong.status_code == 401 and wrong.json()["error"] == "unauthorized"


def test_the_key_opens_the_same_tools_as_the_local_server():
    key = "k" * 43
    with _client(key) as c:
        auth = {**HEADERS, "authorization": f"Bearer {key}"}
        assert c.post("/mcp", json=INIT, headers=auth).status_code == 200
        listed = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth)
        names = {t["name"] for t in listed.json()["result"]["tools"]}
        assert {"setup_status", "outbox_list", "sending_pause"} <= names


def test_the_key_lives_in_the_data_folder_and_can_be_replaced(tmp_path):
    first = mcp_remote.load_key(tmp_path)
    assert mcp_remote.load_key(tmp_path) == first
    assert (tmp_path / mcp_remote.KEY_NAME).stat().st_mode & 0o077 == 0
    second = mcp_remote.new_key(tmp_path)
    assert second != first and mcp_remote.load_key(tmp_path) == second


def test_the_tunnel_address_is_read_from_cloudflared_output():
    line = "2026-11-12 INF |  https://quiet-bird-sells.trycloudflare.com  |"
    assert mcp_remote.TUNNEL_URL.search(line).group(0) == "https://quiet-bird-sells.trycloudflare.com"
