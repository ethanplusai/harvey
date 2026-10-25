"""Google Gemini REST API runtime adapter for Harvey.

Tool-less text-only inference via Google Gemini REST API (v1beta),
using Harvey's existing httpx stack with zero new dependencies.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
import httpx

logger = logging.getLogger("harvey.gemini")

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


class GeminiError(Exception):
    """A Gemini API call failed. ``kind`` drives retry and operator reporting."""

    def __init__(self, message: str, kind: str = "request", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class GeminiResult:
    text: str
    model: str = ""
    duration_ms: int = 0
    usage: dict = field(default_factory=dict)


class GeminiQuotaClient:
    """Gemini quota client returning None (API keys don't expose subscription windows)."""

    async def get_utilization(self) -> dict | None:
        return None


class GeminiClient:
    """Async client for Google Gemini REST API v1beta."""

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.api_key = api_key.strip()
        self.model = model.strip().removeprefix("models/") or DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")

    def fingerprint(self) -> tuple:
        """A hashable representation of configuration for cache invalidation."""
        return (self.api_key, self.model, self.base_url)

    async def check_preflight(self) -> None:
        """Verify API key and connectivity without consuming inference tokens.

        Raises GeminiError on failure.
        """
        if not self.api_key:
            raise GeminiError("GEMINI_API_KEY is not configured in .env.", kind="setup", retryable=False)

        # Probe model metadata via GET /v1beta/models/{model}?key={api_key}
        url = f"{self.base_url}/v1beta/models/{self.model}"
        params = {"key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    return
                self._handle_http_error(resp)
        except GeminiError:
            raise
        except httpx.TimeoutException:
            raise GeminiError(
                f"Gemini preflight timed out contacting {self.base_url}.",
                kind="timeout",
                retryable=True,
            )
        except httpx.NetworkError as e:
            raise GeminiError(
                f"Gemini preflight network error: {e}",
                kind="network",
                retryable=True,
            )
        except Exception as e:
            raise GeminiError(
                f"Gemini preflight failed unexpectedly ({type(e).__name__}).",
                kind="request",
                retryable=False,
            )

    async def preflight(self) -> str:
        """Verify API key and connectivity without consuming inference tokens.

        Returns "" if ready, or an error description.
        """
        try:
            await self.check_preflight()
            return ""
        except GeminiError as e:
            return str(e)
        except Exception as e:
            return f"Gemini preflight failed unexpectedly ({type(e).__name__})."

    async def run(self, prompt: str, timeout: float = 300.0) -> GeminiResult:
        """Execute a text-only, tool-less completion turn against Gemini."""
        if not self.api_key:
            raise GeminiError("GEMINI_API_KEY is missing.", kind="auth", retryable=False)

        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        params = {"key": self.api_key}
        # Omitting "tools" entirely enforces prompt isolation and text-only execution.
        # Note: Sending "tools": [] causes 400 Bad Request on the real Gemini REST API.
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, params=params, json=payload)
                duration_ms = int((time.monotonic() - start) * 1000)

                if resp.status_code != 200:
                    self._handle_http_error(resp)

                data = resp.json()
        except GeminiError:
            raise
        except httpx.TimeoutException:
            raise GeminiError(
                f"Gemini request timed out after {timeout:.0f}s.",
                kind="timeout",
                retryable=True,
            )
        except httpx.NetworkError as e:
            raise GeminiError(
                f"Gemini network error: {e}",
                kind="network",
                retryable=True,
            )
        except Exception as e:
            raise GeminiError(
                f"Gemini request failed unexpectedly ({type(e).__name__}): {e}",
                kind="request",
                retryable=False,
            )

        text = self._extract_text(data)
        usage = self._extract_usage(data)
        model = data.get("modelVersion") or self.model

        return GeminiResult(
            text=text,
            model=model,
            duration_ms=duration_ms,
            usage=usage,
        )

    def _extract_text(self, data: dict) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            block_reason = feedback.get("blockReason")
            if block_reason:
                raise GeminiError(
                    f"Gemini blocked prompt: {block_reason}",
                    kind="request",
                    retryable=False,
                )
            return ""

        first = candidates[0]
        content = first.get("content") or {}
        parts = content.get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        finish_reason = first.get("finishReason")
        if not text and finish_reason and finish_reason != "STOP":
            raise GeminiError(
                f"Gemini response stopped: {finish_reason}",
                kind="request",
                retryable=False,
            )
        return text

    def _extract_usage(self, data: dict) -> dict:
        meta = data.get("usageMetadata") or {}
        usage = {
            "input_tokens": int(meta.get("promptTokenCount") or 0),
            "output_tokens": int(meta.get("candidatesTokenCount") or 0),
        }
        if "cachedContentTokenCount" in meta:
            usage["cache_read_tokens"] = int(meta["cachedContentTokenCount"] or 0)
        return usage

    def _handle_http_error(self, resp: httpx.Response) -> None:
        status = resp.status_code
        err_msg = ""
        err_status = ""
        try:
            err_data = resp.json().get("error", {})
            err_msg = err_data.get("message", "")
            err_status = err_data.get("status", "")
        except Exception:
            err_msg = resp.text[:200]

        if status == 429 or "RESOURCE_EXHAUSTED" in err_msg or err_status == "RESOURCE_EXHAUSTED":
            raise GeminiError(
                f"Gemini rate limit or quota exceeded (429): {err_msg}",
                kind="usage_limit",
                retryable=False,
            )
        if (
            status in (401, 403)
            or err_status in ("UNAUTHENTICATED", "PERMISSION_DENIED")
            or (status == 400 and ("API_KEY" in err_msg or "API key" in err_msg or "API_KEY_INVALID" in err_status))
        ):
            raise GeminiError(
                f"Gemini authentication failed ({status}): {err_msg or 'check GEMINI_API_KEY'}",
                kind="auth",
                retryable=False,
            )
        if status in (500, 502, 503, 504) or err_status in ("UNAVAILABLE", "INTERNAL"):
            raise GeminiError(
                f"Gemini server error ({status}): {err_msg}",
                kind="server",
                retryable=True,
            )
        raise GeminiError(
            f"Gemini request failed ({status}): {err_msg}",
            kind="request",
            retryable=False,
        )
