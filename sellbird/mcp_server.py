"""The MCP server your coding agent launches (`sellbird mcp`).

It speaks MCP over stdio and talks to the running dashboard
(`sellbird dashboard`) over its loopback API with the AGENT's token
(data/agent-token), never yours. The agent can read everything Sellbird
knows, estimate what a discovery run would cost, and pause sending. Every
approval, setting, key, purchase and resume stays yours: the dashboard
enforces that (sellbird/agent_policy.py), not this file.

stdout is the protocol channel, so nothing here prints.
"""

from __future__ import annotations

import functools
import inspect
import logging
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Awaitable, Callable
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from sellbird import dashboard_instance
from sellbird.dashboard_auth import TokenError, load_or_create_token
from sellbird.paths import PROJECT_ROOT
from sellbird.workspace_home import NotOnHosted

logger = logging.getLogger(__name__)

AGENT_TOKEN_NAME = "agent-token"
NOT_RUNNING = ("Sellbird's dashboard isn't running. Ask the user to start it with: "
               "sellbird dashboard")

INSTRUCTIONS = """\
Sellbird is the user's outreach engine: it finds companies and the people who decide,
writes cold email, sends it from the user's own inboxes and handles replies.

You can read everything (today's summary, campaigns, prospects, companies, conversations,
the Outbox, signals, inboxes, usage, setup), estimate what a discovery run would cost, and
pause sending or one inbox. You cannot approve or send email, resume sending, confirm signals, run
discovery, change settings or keys, or delete anything: those are the user's, in the
dashboard (`sellbird dashboard`). When something needs them, say which tab to open
(Today, Outbox, Inboxes, Signals, Discover, Settings) and what to click. Never ask the user for
their dashboard token, and never read data/dashboard-token.
"""


class CoreUnavailable(Exception):
    """The dashboard isn't reachable: not running, no agent token, refusing
    connections, not answering, or a different program on its port."""

    def __init__(self):
        super().__init__(NOT_RUNNING)


def base_url(host: str, port: int) -> str:
    """http://host:port/ with an IPv6 host bracketed."""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/"


class CoreClient:
    """Calls the running dashboard's API as the coding agent."""

    def __init__(self, data_dir: Path, transport: httpx.AsyncBaseTransport | None = None):
        self.data_dir = Path(data_dir)
        self._transport = transport

    def _client(self, base: str) -> httpx.AsyncClient:
        extra = {"transport": self._transport} if self._transport is not None else {}
        return httpx.AsyncClient(base_url=base, timeout=15.0, **extra)

    async def _connection(self) -> tuple[str, str]:
        info = dashboard_instance.read(self.data_dir)
        if info is None:
            raise CoreUnavailable()
        base = base_url(info.host, info.port)
        # Is it really that dashboard? /api/instance needs no token, so the
        # agent's token is sent only once the id matches.
        try:
            async with self._client(base) as http:
                answer = await http.get("/api/instance")
            same = answer.status_code == 200 and answer.json().get("instance") == info.instance
        except (httpx.TransportError, httpx.InvalidURL, OSError, ValueError, AttributeError):
            raise CoreUnavailable() from None
        if not same:
            raise CoreUnavailable()
        try:
            token = load_or_create_token(self.data_dir / AGENT_TOKEN_NAME)
        except TokenError:
            raise CoreUnavailable() from None
        return base, token

    async def _request(self, method: str, path: str, *, json: dict | None = None) -> dict:
        base, token = await self._connection()
        try:
            async with self._client(base) as http:
                response = await http.request(
                    method, path, json=json, headers={"Authorization": f"Bearer {token}"})
        except (httpx.TransportError, httpx.InvalidURL, OSError):
            raise CoreUnavailable() from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code == 409 and isinstance(data, dict) \
                and data.get("error") == "not_on_hosted":
            raise NotOnHosted(data.get("feature", ""))
        if response.status_code >= 400:
            # The dashboard's own refusal (user_only, a missing key) is the answer.
            body = data if isinstance(data, dict) else {}
            return {"ok": False, "status": response.status_code, **body}
        return data if isinstance(data, dict) else {"result": data}

    async def get(self, path: str) -> dict:
        return await self._request("GET", path)

    async def post(self, path: str, body: dict | None = None) -> dict:
        return await self._request("POST", path, json=body)


def default_client() -> CoreClient:
    return CoreClient(PROJECT_ROOT / "data")


# ── A `workspace` argument on every tool, for a server reaching several ──
#
# The local server has one home, so its tools take no workspace. A server
# that reaches several passes a WorkspaceArg: every tool then gains an
# optional trailing `workspace` parameter, the call runs with the resolved
# workspace in `current` (which its client factory reads), and every dict
# result names the workspace it acted on, under `sellbird_workspace`.

WORKSPACE_PARAM_DOC = "Workspace id or name. Your default workspace when left out."
RESULT_WORKSPACE_KEY = "sellbird_workspace"


class WorkspaceNotFound(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class WorkspaceArg:
    resolve: Callable[[str | None], Awaitable[dict]]  # -> {"id", "name"}, or raises WorkspaceNotFound
    current: ContextVar


def _with_workspace(fn, workspace_arg: WorkspaceArg):
    sig = inspect.signature(fn, eval_str=True)
    param = inspect.Parameter("workspace", inspect.Parameter.KEYWORD_ONLY, default=None,
                              annotation=Annotated[str | None, Field(description=WORKSPACE_PARAM_DOC)])

    @functools.wraps(fn)
    async def wrapper(*args, workspace: str | None = None, **kwargs):
        try:
            resolved = await workspace_arg.resolve(workspace)
        except WorkspaceNotFound as e:
            return {"ok": False, "message": e.message}
        token = workspace_arg.current.set(resolved)
        try:
            result = await fn(*args, **kwargs)
        finally:
            workspace_arg.current.reset(token)
        return {**result, RESULT_WORKSPACE_KEY: resolved} if isinstance(result, dict) else result

    wrapper.__signature__ = sig.replace(parameters=[*sig.parameters.values(), param])
    wrapper.__annotations__ = {**{p.name: p.annotation for p in sig.parameters.values()},
                               "workspace": param.annotation, "return": sig.return_annotation}
    return wrapper


def _add_workspace_arg(server: MCPServer, workspace_arg: WorkspaceArg) -> None:
    """Every tool registered after this takes `workspace` (see above)."""
    register_tool = server.tool

    def tool(*args, **kwargs):
        register = register_tool(*args, **kwargs)
        return lambda fn: register(_with_workspace(fn, workspace_arg))
    server.tool = tool


# ── The server ──

def build_server(client_factory: Callable[[], CoreClient] = default_client, *,
                 workspace_arg: WorkspaceArg | None = None,
                 unavailable_message: str | None = None,
                 instructions: str = INSTRUCTIONS) -> MCPServer:
    server = MCPServer("sellbird", instructions=instructions)
    if workspace_arg is not None:
        _add_workspace_arg(server, workspace_arg)

    async def guarded(call):
        """Run one tool's call; a tool never raises, it answers ok: false."""
        try:
            return await call(client_factory())
        except CoreUnavailable as e:
            return {"ok": False, "message": unavailable_message or str(e)}
        except NotOnHosted as e:
            return {"ok": False, "message": e.payload["message"]}
        except Exception:  # the detail goes to stderr, never to the agent
            logger.exception("Sellbird MCP tool failed")
            return {"ok": False, "message": unavailable_message or NOT_RUNNING}

    def read(path: str):
        return guarded(lambda c: c.get(path))

    @server.tool()
    async def today() -> dict:
        """What needs the user now, and the pipeline at a glance. Start here."""
        return await read("/api/today")

    @server.tool()
    async def setup_status() -> dict:
        """Which setup steps are done and what's still missing, from install to sending."""
        return await read("/api/setup-status")

    @server.tool()
    async def stats() -> dict:
        """Pipeline counts: companies, prospects, campaigns, conversations, sends."""
        return await read("/api/stats")

    @server.tool()
    async def activity() -> dict:
        """What Sellbird did recently, newest first."""
        return await read("/api/activity")

    @server.tool()
    async def usage() -> dict:
        """How much of the Claude or Codex subscription Sellbird has used, and its own calls."""
        return await read("/api/usage")

    @server.tool()
    async def campaigns_list() -> dict:
        """Every campaign with its sequence and status."""
        return await read("/api/campaigns")

    @server.tool()
    async def prospects_list() -> dict:
        """The newest prospects (up to 200) with their status and email verification."""
        return await read("/api/prospects")

    @server.tool()
    async def companies_list() -> dict:
        """The companies Sellbird has found, with their fit."""
        return await read("/api/companies")

    @server.tool()
    async def company_contacts(company_id: str) -> dict:
        """The people Sellbird found at one company."""
        return await read(f"/api/companies/{quote(company_id, safe='')}/contacts")

    @server.tool()
    async def company_profile(company_id: str) -> dict:
        """What Sellbird read off one company's website: its signals and profile."""
        return await read(f"/api/companies/{quote(company_id, safe='')}/profile")

    @server.tool()
    async def conversations_list() -> dict:
        """Reply threads, with their stage and whether they need the user."""
        return await read("/api/conversations")

    @server.tool()
    async def outbox_list() -> dict:
        """Email waiting for approval, approved and scheduled, and recently sent or failed."""
        return await read("/api/outbox")

    @server.tool()
    async def signals_list() -> dict:
        """The signals Sellbird can collect, which the user confirmed, and what each costs."""
        return await read("/api/signals")

    @server.tool()
    async def discover_providers() -> dict:
        """The discovery sources, what each costs and which keys each needs."""
        return await read("/api/discover/providers")

    @server.tool()
    async def discover_estimate(provider: str, cities: list[str] | None = None,
                                depth: int = 30, limit: int = 100, preset: str = "") -> dict:
        """The searches a discovery run would make and what they'd cost. Runs nothing:
        only the user starts a run, on the Discover tab. `provider` is a key from
        discover_providers; cities default to the ICP's."""
        body = {"provider": provider, "cities": cities or [], "depth": depth,
                "limit": limit, "preset": preset}
        return await guarded(lambda c: c.post("/api/discover/estimate", body))

    @server.tool()
    async def sending_status() -> dict:
        """Whether sending is paused and why, and whether email waits for approval."""
        async def call(c: CoreClient) -> dict:
            live = await c.get("/api/live-status")
            mode = await c.get("/api/sending-mode")
            if live.get("ok") is False:
                return live
            return {"live": live, "mode": mode}
        return await guarded(call)

    @server.tool()
    async def sending_pause() -> dict:
        """Pause all sending now. Safe to repeat. Only the user can resume, in the dashboard."""
        async def call(c: CoreClient) -> dict:
            result = await c.post("/api/sending/pause")
            if result.get("ok") is False:
                return result
            return {**result, "paused": True,
                    "message": "Sending is paused. Only the user can resume it, in the dashboard."}
        return await guarded(call)

    @server.tool()
    async def inboxes_list() -> dict:
        """Every sending inbox: the main mailbox and the extra ones the user connected,
        whether each is sending, paused or not connected, and today's cold sends
        against its cap."""
        return await read("/api/inboxes")

    @server.tool()
    async def inbox_pause(inbox_id: str, reason: str = "") -> dict:
        """Pause one extra inbox (an id from inboxes_list). Its unsent first emails
        move to the other inboxes. Only the user can resume it, in the dashboard's
        Inboxes tab. To stop every inbox, use sending_pause."""
        return await guarded(lambda c: c.post(
            f"/api/inboxes/{quote(inbox_id, safe='')}/pause", {"reason": reason}))

    # Tools that installed extensions add (sellbird.extensions).
    from sellbird import extensions
    for register in extensions.registry().mcp_tools.values():
        register(server, client_factory, guarded)

    return server


def run_stdio() -> None:
    # stdout carries the protocol; logs go to stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
    build_server().run("stdio")
