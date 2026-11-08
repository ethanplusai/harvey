"""Opt-in, API-billed no-tools backend for a local Codex Responses router.

This does NOT use the subscription-only Codex backend or read Codex login,
configuration, MCP servers, hooks, plugins, or workspace instructions.
"""

import asyncio
import ipaddress
import re
import time
from urllib.parse import urlsplit

import httpx

from sellbird.codex import CodexError, CodexResult


def validate_router_url(value: str) -> str:
    url = urlsplit(value)
    try:
        local = ipaddress.ip_address(url.hostname or "").is_loopback
        port = url.port
    except ValueError:
        local, port = False, None
    if (not local or url.scheme not in ("http", "https") or not port
            or url.username or url.password or url.query or url.fragment
            or url.path.rstrip("/") != "/v1"):
        raise ValueError("router_base_url must be a loopback IP URL with port and /v1.")
    return value.rstrip("/")


class RouterQuotaClient:
    """No subscription quota is available. The existing daily call cap applies."""

    async def get_utilization(self):
        return None

    def invalidate(self):
        pass


class RouterClient:
    def __init__(self, model: str, reasoning_effort: str = "",
                 base_url: str = "http://127.0.0.1:20129/v1", transport=None):
        self.base_url = validate_router_url(base_url)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}", model):
            raise ValueError("An explicit valid router model ID is required.")
        if reasoning_effort not in ("", "minimal", "low", "medium", "high", "xhigh"):
            raise ValueError("Invalid reasoning effort.")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._transport = transport
        self._lock = asyncio.Lock()

    def fingerprint(self):
        return self.base_url, self.model, self.reasoning_effort

    async def preflight(self, timeout: float = 10.0):
        """Reachability only: do not send a paid prompt or pretend to verify auth."""
        url = urlsplit(self.base_url)
        try:
            async with asyncio.timeout(timeout):
                _, writer = await asyncio.open_connection(url.hostname, url.port)
                writer.close()
                await writer.wait_closed()
        except (OSError, TimeoutError):
            raise CodexError("Local router is not reachable.", kind="transport",
                             retryable=True) from None

    async def run(self, prompt: str, timeout: float, skip_if=None) -> CodexResult:
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                async with self._lock:
                    if skip_if and skip_if():
                        raise CodexError("Router call paused.", kind="paused")
                    payload = {
                        "model": self.model,
                        "instructions": "Return an answer as text. Do not use tools.",
                        "input": [{"role": "user", "content": [
                            {"type": "input_text", "text": prompt}]}],
                        "tools": [], "tool_choice": "none",
                        "parallel_tool_calls": False, "stream": False, "store": False,
                    }
                    if self.reasoning_effort:
                        payload["reasoning"] = {"effort": self.reasoning_effort}
                    async with httpx.AsyncClient(
                        transport=self._transport, trust_env=False,
                        follow_redirects=False, timeout=timeout,
                    ) as client:
                        response = await client.post(
                            self.base_url + "/responses", json=payload)
                    if response.status_code != 200:
                        status = response.status_code
                        kind = ("auth" if status in (401, 403) else
                                "usage_limit" if status == 429 else
                                "setup" if status in (400, 404, 422) else "transport")
                        # A server error can occur after upstream billing.
                        raise CodexError(f"Router request failed (HTTP {status}).",
                                         kind=kind)
                    try:
                        data = response.json()
                        return self._result(data, started)
                    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                        raise CodexError("Router returned an invalid response.") from None
        except (TimeoutError, httpx.TimeoutException):
            # Timeout can occur after billing; avoid an automatic duplicate charge.
            raise CodexError("Router call timed out; billing outcome unknown.",
                             kind="timeout") from None
        except httpx.ConnectError:
            raise CodexError("Cannot connect to local router.", kind="transport",
                             retryable=True) from None
        except httpx.HTTPError:
            raise CodexError("Router transport failed.", kind="transport",
                             retryable=False) from None

    def _result(self, data: dict, started: float) -> CodexResult:
        if not isinstance(data, dict) or data.get("status") != "completed":
            raise CodexError("Router response did not complete.")
        # Routers normally strip the provider prefix from the upstream model.
        # Accept only the exact configured ID or that exact unprefixed ID;
        # never silently attribute an unrelated model to the chosen one.
        unprefixed = self.model.split("/", 1)[-1]
        if data.get("model") not in (self.model, unprefixed):
            raise CodexError("Router returned a different or missing model ID.",
                             kind="setup")
        output = data.get("output")
        if not isinstance(output, list):
            raise CodexError("Router output is missing.")
        text = []
        for item in output:
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                raise CodexError("Router returned forbidden non-text output.",
                                 kind="isolation")
            for part in item.get("content", []):
                if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise CodexError("Router returned forbidden message content.",
                                     kind="isolation")
                text.append(part["text"])
        if not text or not "\n".join(text).strip():
            raise CodexError("Router returned no answer.")
        usage = data.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        def number(value):
            return max(0, int(value or 0))
        cached = number(details.get("cached_tokens"))
        return CodexResult(
            text="\n".join(text), model=self.model,
            thread_id=str(data.get("id") or ""),
            duration_ms=int((time.monotonic() - started) * 1000),
            usage={
                "input_tokens": max(0, number(usage.get("input_tokens")) - cached),
                "cache_read_tokens": cached,
                "cache_creation_tokens": number(details.get("cache_write_tokens")),
                "output_tokens": number(usage.get("output_tokens")),
            },
        )
