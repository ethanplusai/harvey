"""What the coding agent may call on the dashboard API.

The dashboard accepts two tokens (dashboard_auth): yours, used by the
dashboard, and the agent's, used by the MCP server (sellbird/mcp_server.py).
With the agent's token a request may reach only the routes listed here:
reading, estimating a cost, and pausing sending. Everything else, every
approval, setting, key, purchase, resume or deletion, is yours alone and
answers 403 to the agent.

The list is an allowlist on purpose: a route added to the dashboard later is
yours until someone decides the agent may call it. An extension adds its own
agent routes with ``registry.add_agent_route`` (sellbird.extensions).
"""

from __future__ import annotations

import json
import re

# (method, path pattern). Patterns are full matches; {id} segments are [^/]+.
AGENT_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", r"/api/today"),
    ("GET", r"/api/stats"),
    ("GET", r"/api/trend"),
    ("GET", r"/api/activity"),
    ("GET", r"/api/usage"),
    ("GET", r"/api/live-status"),
    ("GET", r"/api/setup-status"),
    ("GET", r"/api/sending-mode"),
    ("GET", r"/api/sellbird/status"),
    ("GET", r"/api/runs"),
    ("GET", r"/api/campaigns"),
    ("GET", r"/api/companies"),
    ("GET", r"/api/companies/[^/]+/contacts"),
    ("GET", r"/api/companies/[^/]+/profile"),
    ("GET", r"/api/prospects"),
    ("GET", r"/api/conversations"),
    ("GET", r"/api/outbox"),
    ("GET", r"/api/signals"),
    ("GET", r"/api/discover/providers"),
    ("GET", r"/api/inboxes"),
    ("POST", r"/api/discover/estimate"),   # computes a price, runs nothing
    ("POST", r"/api/sending/pause"),       # never resume: that's yours
    ("POST", r"/api/inboxes/[^/]+/pause"), # one inbox; resuming is yours too
)

USER_ONLY = {
    "error": "user_only",
    "message": "Only the user can do this, in the Sellbird dashboard.",
}


def _compile(routes) -> list[tuple[str, re.Pattern]]:
    return [(method.upper(), re.compile(pattern)) for method, pattern in routes]


_BUILT_IN = _compile(AGENT_ROUTES)


def agent_may(method: str, path: str, extra=()) -> bool:
    method = "GET" if method.upper() == "HEAD" else method.upper()
    return any(m == method and p.fullmatch(path) for m, p in (*_BUILT_IN, *extra))


class AgentPolicy:
    """ASGI middleware, inside DashboardGuard: a request made with the agent's
    token reaches only the allowed routes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (scope.get("state") or {}).get("principal") != "agent":
            return await self.app(scope, receive, send)
        from sellbird import extensions
        extra = _compile(extensions.registry().agent_routes)
        if agent_may(scope["method"], scope["path"], extra):
            return await self.app(scope, receive, send)
        body = json.dumps(USER_ONLY).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
