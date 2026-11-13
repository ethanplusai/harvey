"""Sellbird's MCP tools over the web, for an agent that lives in the cloud.

`sellbird mcp` speaks stdio, so only an agent on this computer (Claude Code,
Codex) can launch it. Grok Bot runs on xAI's cloud computer and can't reach
this one, so `sellbird grok` serves the same tools over streamable HTTP and
opens a tunnel to them.

The tools are exactly the local server's: they call the running dashboard with
the agent's token, and the dashboard still refuses every approval, purchase,
key and resume (sellbird/agent_policy.py). What this adds is a door from the
internet, so every request must carry this server's own key
(data/remote-mcp-key) as `Authorization: Bearer <key>`. Without it the answer
is 401 and nothing runs.
"""

from __future__ import annotations

import hmac
import re
import shutil
import subprocess
import threading
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

from sellbird.dashboard_auth import load_or_create_token

KEY_NAME = "remote-mcp-key"
DEFAULT_PORT = 5590
MCP_PATH = "/mcp"
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def key_path(data_dir: Path) -> Path:
    return Path(data_dir) / KEY_NAME


def load_key(data_dir: Path) -> str:
    """This server's key, made on first use (owner-only file, like the agent token)."""
    return load_or_create_token(key_path(data_dir))


def new_key(data_dir: Path) -> str:
    """Replace the key: the old one stops working at once."""
    path = key_path(data_dir)
    path.unlink(missing_ok=True)
    return load_or_create_token(path)


class RequireKey:
    """ASGI middleware: refuse any HTTP request without the bearer key."""

    def __init__(self, app: ASGIApp, key: str):
        self.app = app
        self._expected = f"Bearer {key}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        given = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(given, self._expected):
            body = b'{"error":"unauthorized","message":"Missing or wrong Sellbird key."}'
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_app(key: str, server=None) -> ASGIApp:
    """The local MCP server's tools as a streamable-HTTP app at /mcp, behind the key."""
    from mcp.server.transport_security import TransportSecuritySettings

    from sellbird.mcp_server import build_server

    server = server or build_server()
    # The tunnel's host name changes on every start, so host checks can't pin it;
    # the bearer key is what keeps strangers out.
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH, stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return RequireKey(app, key)


class Tunnel:
    """A Cloudflare quick tunnel (no account) to a local port, while it runs."""

    def __init__(self, port: int):
        self.port = port
        self.url: str | None = None
        self._proc: subprocess.Popen | None = None
        self._found = threading.Event()

    @staticmethod
    def available() -> bool:
        return shutil.which("cloudflared") is not None

    def start(self, timeout: float = 30.0) -> str | None:
        self._proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        threading.Thread(target=self._watch, daemon=True).start()
        self._found.wait(timeout)
        return self.url

    def _watch(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            if self.url is None:
                match = TUNNEL_URL.search(line)
                if match:
                    self.url = match.group(0)
                    self._found.set()

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
