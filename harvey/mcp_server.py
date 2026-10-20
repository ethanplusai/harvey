"""MCP server exposing Harvey's dashboard API to AI agents over stdio.

Lets an agent (Hermes, Claude Desktop, …) drive and inspect Harvey — status,
start/stop, prospects, outbox approval, sending pause/resume — through the
Model Context Protocol. Each tool is a thin httpx call to the dashboard's
JSON API (default ``http://localhost:5555``, override with ``HARVEY_API_URL``).

Run directly (``python -m harvey.mcp_server``) or via the ``harvey-mcp``
console script, then point an MCP client at it:

.. code-block:: yaml

    mcp_servers:
      harvey:
        command: "harvey-mcp"
"""
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("HARVEY_API_URL", "http://localhost:5555")

mcp = FastMCP("harvey")


def _url(path: str) -> str:
    return f"{BASE_URL.rstrip('/')}{path}"


async def _get(path: str):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(_url(path))
        response.raise_for_status()
        return response.json()


async def _post(path: str):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(_url(path))
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_status() -> dict:
    """Harvey's heartbeat-loop status (running, pid, started_at)."""
    return await _get("/api/harvey/status")


@mcp.tool()
async def start() -> dict:
    """Start Harvey's heartbeat loop."""
    return await _post("/api/harvey/start")


@mcp.tool()
async def stop() -> dict:
    """Stop Harvey's heartbeat loop."""
    return await _post("/api/harvey/stop")


@mcp.tool()
async def get_logs() -> dict:
    """Recent Harvey log lines."""
    return await _get("/api/harvey/logs")


@mcp.tool()
async def get_stats() -> dict:
    """Dashboard stats (prospects, emails sent, replies, meetings)."""
    return await _get("/api/stats")


@mcp.tool()
async def get_usage() -> dict:
    """Brain usage / budget counters."""
    return await _get("/api/usage")


@mcp.tool()
async def get_prospects() -> list:
    """The prospect list (up to 200, newest first)."""
    return await _get("/api/prospects")


@mcp.tool()
async def get_outbox() -> dict:
    """Outbox queue + kill-switch state (pending/approved/sent/failed)."""
    return await _get("/api/outbox")


@mcp.tool()
async def approve_outbox_item(item_id: str) -> dict:
    """Approve one queued outbox email by id."""
    return await _post(f"/api/outbox/{item_id}/approve")


@mcp.tool()
async def reject_outbox_item(item_id: str) -> dict:
    """Reject one queued outbox email by id."""
    return await _post(f"/api/outbox/{item_id}/reject")


@mcp.tool()
async def approve_all_outbox() -> dict:
    """Approve every queued outbox email."""
    return await _post("/api/outbox/approve-all")


@mcp.tool()
async def set_sending_paused(paused: bool) -> dict:
    """Pause (True) or resume (False) outgoing email sending."""
    return await _post(f"/api/sending/{'pause' if paused else 'resume'}")


@mcp.tool()
async def get_companies() -> list:
    """Discovered companies."""
    return await _get("/api/companies")


@mcp.tool()
async def get_campaigns() -> list:
    """Campaigns (up to 100)."""
    return await _get("/api/campaigns")


@mcp.tool()
async def get_conversations() -> list:
    """Email conversations (up to 100)."""
    return await _get("/api/conversations")


@mcp.tool()
async def get_activity() -> list:
    """Recent agent actions (up to 100)."""
    return await _get("/api/activity")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
