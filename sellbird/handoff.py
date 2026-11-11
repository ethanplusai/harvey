"""When someone says yes to your offer, hand them to the system that delivers it.

Sellbird finds people, writes the first email and reads the replies. What
happens after "yes, send it" belongs to whatever you sell: a site builder
starts a build, an agency books an audit, a CRM gets a new deal. So when a
reply accepts the offer (handler intent ``accepted_offer``), Sellbird calls
one tool on another system's MCP server, configured in ``handoff``::

    handoff:
      mcp_url: https://example.com/mcp      # an MCP server over HTTP, or
      mcp_command: ["npx", "-y", "some-mcp"] # one Sellbird starts locally
      token_env: EXAMPLE_MCP_TOKEN           # .env variable with its bearer token
      tool: create_site
      arguments:
        company: "{company}"
        website: "{website}"
        email: "{email}"
        contact: "{first_name} {last_name}"
        note: "{reply}"

Argument values may use {first_name} {last_name} {email} {title} {company}
{website} {reply} {offer}. Whatever the tool answers is kept with the
conversation, and the reply Sellbird drafts (for your approval) can use it,
such as the link to what was made.

With no handoff configured, nothing is called: the person shows up as
"said yes to your offer" on Conversations and in the agent's
``offers_accepted`` tool, for you or your coding agent to act on.
"""

import logging
import os
import re

logger = logging.getLogger("sellbird.handoff")

CALL_TIMEOUT = 60.0
MAX_RESULT_CHARS = 2000
_FIELD_RE = re.compile(r"\{(\w+)\}")
FIELDS = ("first_name", "last_name", "email", "title", "company", "website", "reply", "offer")


def configured(config) -> bool:
    h = getattr(config, "handoff", None)
    return bool(h and h.tool and (h.mcp_url or h.mcp_command))


def fill(arguments: dict, values: dict) -> dict:
    """``arguments`` with each {field} replaced; unknown fields become ""."""
    def one(v):
        if isinstance(v, str):
            return _FIELD_RE.sub(lambda m: str(values.get(m.group(1), "")), v).strip()
        if isinstance(v, dict):
            return {k: one(x) for k, x in v.items()}
        if isinstance(v, list):
            return [one(x) for x in v]
        return v
    return {k: one(v) for k, v in (arguments or {}).items()}


def values_for(prospect, company, reply_text: str, offer: str) -> dict:
    website = ""
    if company is not None:
        website = getattr(company, "website", "") or getattr(company, "domain", "") or ""
    return {
        "first_name": prospect.first_name or "", "last_name": prospect.last_name or "",
        "email": prospect.email or "", "title": prospect.title or "",
        "company": prospect.company or "", "website": website,
        "reply": (reply_text or "")[:1500], "offer": offer or "",
    }


def _result_text(result) -> str:
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    structured = getattr(result, "structured_content", None)
    if not parts and structured:
        parts.append(str(structured))
    return "\n".join(parts).strip()[:MAX_RESULT_CHARS]


async def call_tool(h, arguments: dict, env: dict | None = None) -> dict:
    """Call ``h.tool`` with ``arguments`` on the configured server.

    Returns {"ok": True, "result": text} or {"ok": False, "error": why}.
    Never raises: a handoff that fails leaves the person on Conversations
    as "said yes to your offer", for you to act on."""
    from mcp import ClientSession
    env = env if env is not None else os.environ
    try:
        if h.mcp_url:
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            headers = {}
            token = (env.get(h.token_env) or "").strip() if h.token_env else ""
            if h.token_env and not token:
                return {"ok": False, "error": f"{h.token_env} isn't set in .env"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with create_mcp_http_client(headers=headers) as http:
                async with streamable_http_client(h.mcp_url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(h.tool, arguments,
                                                         read_timeout_seconds=CALL_TIMEOUT)
        else:
            from mcp.client.stdio import StdioServerParameters, stdio_client
            params = StdioServerParameters(command=h.mcp_command[0], args=list(h.mcp_command[1:]))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(h.tool, arguments,
                                                     read_timeout_seconds=CALL_TIMEOUT)
    except Exception as e:  # any transport or server failure
        logger.warning(f"Handoff: calling {h.tool} failed: {e}")
        return {"ok": False, "error": f"couldn't reach the handoff server ({type(e).__name__})"}
    text = _result_text(result)
    if getattr(result, "is_error", False):
        return {"ok": False, "error": text or f"{h.tool} returned an error"}
    return {"ok": True, "result": text}


async def hand_off(config, prospect, company, reply_text: str) -> dict | None:
    """Hand an accepted offer to the configured system, or None when no
    handoff is configured."""
    if not configured(config):
        return None
    h = config.handoff
    offer = getattr(getattr(config.product, "offer", None), "entry", "") or ""
    arguments = fill(h.arguments, values_for(prospect, company, reply_text, offer))
    outcome = await call_tool(h, arguments)
    return {**outcome, "tool": h.tool, "arguments": arguments}
