"""OpenCode Go adapter — Harvey's brain on an OpenCode Go subscription.

OpenCode Go exposes OpenAI-, Anthropic-, Responses- and Google-shaped
inference endpoints under https://opencode.ai/inference/go. Harvey calls them
directly: one HTTPS request per prompt, no CLI, no tools, no session store.
That is deliberately the smallest possible surface — the model sees only the
prompt Harvey sends, and returns only text.

Every Go request needs:

- ``Authorization: Bearer $OPENCODE_API_KEY``
- ``x-opencode-session``: a stable id per conversation (Harvey uses the agent
  label, e.g. ``harvey-writer``), so Go can route and cache the prompt prefix
- a client user agent, not a generic HTTP-library one

Go also reports its own allowance accounting in ``usage.opencode``. For a Go
call, ``allowance`` is the amount consumed from that model's monthly plan;
Harvey records it and budgets against the plan's 5-hour, weekly and monthly
allowances. See https://opencode.ai/docs/go/#where-can-i-use-it.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches

import httpx
from dotenv import load_dotenv

from harvey.paths import PROJECT_ROOT

DEFAULT_BASE_URL = "https://opencode.ai/inference/go"
DEFAULT_MODEL = "deepseek-v4.1-flash"
API_KEY_ENV = "OPENCODE_API_KEY"
FALLBACK_API_KEY_ENV = "OPENCODE_GO_API_KEY"
USER_AGENT = "harvey/0.3"

MODELS_TIMEOUT = 30.0
REQUEST_TIMEOUT = 300.0
CATALOG_TTL_SECONDS = 3600.0

# Go's documented endpoints for the models that do not speak OpenAI chat
# completions. Everything else is OpenAI-compatible chat.
ANTHROPIC_MODELS = {
    "claude-haiku-5-5",
    "minimax-m3",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
}
RESPONSES_MODELS = {
    "grok-4.6",
    "grok-4.7",
    "gpt-5.6-luna",
    "gpt-6-luna",
    "muse-spark-1.2-contributor",
    "muse-spark-1.3-contributor",
}
GOOGLE_MODELS = {"gemini-4-argon-contributor"}

# Prices are per 1M tokens and monthly limits are the Go plan's allowance for
# that model, both from https://opencode.ai/docs/go/ (verified 2026-10-25).
# A model missing here still works; Harvey then budgets by call count and
# records no dollar estimate (the API's own ``allowance`` is still used when
# it is present).
MODEL_INFO: dict[str, dict] = {
    "deepseek-v4.1-flash": {
        "monthly": 80, "input": 0.15, "output": 0.60, "cache_read": 0.003,
        "peak": {"input": 0.30, "output": 1.20, "cache_read": 0.006},
    },
    "deepseek-v4-pro": {
        "monthly": 15, "input": 0.66, "output": 1.98, "cache_read": 0.022,
        "peak": {"input": 1.32, "output": 3.96, "cache_read": 0.044},
    },
    # GLM-5.1 is in the live catalog but not in the published Go limit table;
    # its allowance is unknown, so the budget falls back to call counting.
    "glm-5.1": {"monthly": None, "input": 1.40, "output": 4.40, "cache_read": 0.26},
    "glm-5.2": {"monthly": 60, "input": 1.40, "output": 4.40, "cache_read": 0.26},
    "glm-5.3": {"monthly": 15, "input": 1.40, "output": 4.40, "cache_read": 0.26},
    "glm-5.3-flash": {"monthly": 60, "input": 0.15, "output": 0.50, "cache_read": 0.03},
    "kimi-k2.6": {"monthly": None, "input": 0.95, "output": 4.00, "cache_read": 0.16},
    "kimi-k2.7-code": {"monthly": 60, "input": 0.95, "output": 4.00, "cache_read": 0.19},
    "kimi-k3": {"monthly": 15, "input": 3.00, "output": 15.00, "cache_read": 0.30},
    "longcat-2.0": {"monthly": 60, "input": 0.30, "output": 1.20, "cache_read": 0.006},
    "longcat-2.5-preview-free": {"monthly": 0, "input": 0.0, "output": 0.0, "cache_read": 0.0},
    "beam-free": {"monthly": 0, "input": 0.0, "output": 0.0, "cache_read": 0.0},
    "step-5-preview": {"monthly": 15, "input": 1.00, "output": 2.70, "cache_read": 0.10},
    "mimo-v2.5": {"monthly": 60, "input": 0.14, "output": 0.28, "cache_read": 0.0028},
    "mimo-v2.5-pro": {"monthly": 15, "input": 0.435, "output": 0.87, "cache_read": 0.003625},
    "mimo-v2.6-flash": {"monthly": 60, "input": 0.14, "output": 0.28, "cache_read": 0.0028},
    "mimo-v2.6-pro": {"monthly": 15, "input": 0.435, "output": 0.87, "cache_read": 0.003625},
    "minimax-m2.5": {"monthly": None, "input": 0.30, "output": 1.20, "cache_read": 0.06},
    "minimax-m2.7": {"monthly": None, "input": 0.30, "output": 1.20, "cache_read": 0.06},
    "minimax-m3": {"monthly": 60, "input": 0.30, "output": 1.20, "cache_read": 0.06},
    "minimax-m3.1-flash": {"monthly": 20, "input": 0.15, "output": 0.60, "cache_read": 0.015},
    "muse-spark-1.2-contributor": {
        "monthly": 60, "input": 0.10, "output": 0.20, "cache_read": 0.002,
    },
    "muse-spark-1.3-contributor": {
        "monthly": 60, "input": 0.10, "output": 0.20, "cache_read": 0.002,
    },
    "qwen3.6-plus": {"monthly": 60, "input": 0.50, "output": 3.00, "cache_read": 0.05},
    "qwen3.7-max": {"monthly": 15, "input": 2.50, "output": 7.50, "cache_read": 0.50},
    "qwen3.7-plus": {"monthly": 60, "input": 0.40, "output": 1.60, "cache_read": 0.04},
    "qwen3.8-flash": {"monthly": 30, "input": 0.15, "output": 0.47, "cache_read": 0.016},
    "qwen3.8-max": {"monthly": 15, "input": 2.00, "output": 6.00, "cache_read": 0.25},
    "hy3": {"monthly": 60, "input": 0.14, "output": 0.58, "cache_read": 0.035},
    "hy4-preview": {"monthly": 30, "input": 0.834, "output": 2.501, "cache_read": 0.042},
    "grok-4.6": {"monthly": None, "input": 2.00, "output": 6.00, "cache_read": 0.50},
    "grok-4.7": {"monthly": 15, "input": 2.00, "output": 6.00, "cache_read": 0.50},
    "gpt-5.6-luna": {"monthly": 15, "input": 0.20, "output": 1.20, "cache_read": 0.02},
    "gpt-6-luna": {"monthly": 15, "input": 0.10, "output": 0.50, "cache_read": 0.01},
    "claude-haiku-5-5": {"monthly": 15, "input": 0.10, "output": 0.50, "cache_read": 0.01},
    "gemini-4-argon-contributor": {
        "monthly": 60, "input": 4.00, "output": 20.00, "cache_read": 0.20,
    },
}

# The model's monthly Go allowance, or None when it is free/unlimited or its
# limit is not published (the budget then falls back to call counting).
def monthly_limit(model: str) -> float | None:
    info = MODEL_INFO.get(model)
    if not info:
        return None
    limit = float(info.get("monthly") or 0.0)
    return limit or None


def model_style(model: str) -> str:
    if model in ANTHROPIC_MODELS:
        return "anthropic"
    if model in RESPONSES_MODELS:
        return "responses"
    if model in GOOGLE_MODELS:
        return "google"
    return "chat"


def chat_models() -> list[str]:
    """Model ids that use the OpenAI-compatible chat endpoint."""
    return [m for m in MODEL_INFO if model_style(m) == "chat"]


class OpenCodeError(Exception):
    """An OpenCode Go call failed. ``kind`` drives retry and the pause reason."""

    def __init__(self, message: str, kind: str = "protocol",
                 retryable: bool = False, retry_after: float = 0.0):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class OpenCodeResult:
    text: str
    model: str
    request_id: str
    duration_ms: int
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    billing_source: str = ""


# Process-wide catalog cache: the list is public, so it is keyed by base URL
# and refreshed hourly. A test can call clear_catalog_cache().
_CATALOG_CACHE: dict[str, tuple[float, list[str]]] = {}


def clear_catalog_cache() -> None:
    _CATALOG_CACHE.clear()


def api_key_from_env() -> str:
    """The Go key from the environment (loading .env first)."""
    try:
        load_dotenv(PROJECT_ROOT / ".env")
        load_dotenv()
    except Exception:
        pass
    for name in (API_KEY_ENV, FALLBACK_API_KEY_ENV):
        value = os.getenv(name, "").strip()
        if value:
            return value.strip().strip('"').strip("'")
    return ""


def _session_header(session_id: str) -> str:
    """A header-safe, stable session id (Go requires x-opencode-session)."""
    value = re.sub(r"[^A-Za-z0-9._:-]+", "-", (session_id or "harvey-brain").strip())
    return (value or "harvey-brain")[:80]


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _int(value) -> int:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else 0


def _is_deepseek_peak(now: datetime | None = None) -> bool:
    """DeepSeek's peak hours: 01-04 and 06-10 UTC, Monday-Friday."""
    now = now or datetime.now(timezone.utc)
    return now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10)


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, now: datetime | None = None) -> float:
    """List-price estimate, used only when the API returns no allowance/cost."""
    info = MODEL_INFO.get(model)
    if not info:
        return 0.0
    prices = info
    peak = info.get("peak")
    if isinstance(peak, dict) and _is_deepseek_peak(now):
        prices = {**info, **peak}
    cost = (
        input_tokens * float(prices.get("input") or 0.0)
        + output_tokens * float(prices.get("output") or 0.0)
        + cache_read_tokens * float(prices.get("cache_read") or 0.0)
    ) / 1_000_000
    return round(cost, 8)


class OpenCodeClient:
    """One OpenCode Go model, called over HTTPS."""

    def __init__(
        self,
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        max_tokens: int = 8192,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.model = (model or DEFAULT_MODEL).strip()
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.max_tokens = int(max_tokens or 8192)
        self.transport = transport
        self.timeout = float(timeout or REQUEST_TIMEOUT)

    # ── auth / catalog ──

    def _key(self) -> str:
        key = self.api_key or api_key_from_env()
        if not key:
            raise OpenCodeError(
                f"OpenCode Go needs an API key. Add {API_KEY_ENV} to .env "
                "(OpenCode Console → Go → API key).",
                kind="setup",
            )
        return key

    def fingerprint(self) -> tuple:
        """What a health pause is tied to: base URL, model and a key hash."""
        import hashlib
        key = self.api_key or api_key_from_env()
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else ""
        return (self.base_url, self.model, digest)

    def _headers(self, session_id: str = "") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "x-opencode-session": _session_header(session_id),
        }

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0),
            transport=self.transport,
        )

    async def list_models(self, timeout: float = MODELS_TIMEOUT) -> list[str]:
        """The Go model catalog, cached for an hour."""
        cached = _CATALOG_CACHE.get(self.base_url)
        if cached and time.monotonic() - cached[0] < CATALOG_TTL_SECONDS:
            return cached[1]
        try:
            async with self._client(timeout) as client:
                response = await client.get(
                    f"{self.base_url}/v1/models",
                    headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                )
        except httpx.TimeoutException:
            raise OpenCodeError("OpenCode Go model catalog timed out.",
                                kind="timeout", retryable=True)
        except httpx.HTTPError as e:
            raise OpenCodeError(
                f"OpenCode Go model catalog request failed ({type(e).__name__}).",
                kind="transport", retryable=True,
            )
        if response.status_code != 200:
            raise _classify_error(response)
        try:
            body = response.json()
        except ValueError:
            raise OpenCodeError("OpenCode Go returned an unreadable model catalog.",
                                kind="protocol", retryable=True)
        data = body.get("data") if isinstance(body, dict) else None
        models = [
            str(item.get("id")) for item in (data or [])
            if isinstance(item, dict) and item.get("id")
        ]
        if not models:
            raise OpenCodeError("OpenCode Go returned an empty model catalog.",
                                kind="protocol", retryable=True)
        _CATALOG_CACHE[self.base_url] = (time.monotonic(), models)
        return models

    async def preflight(self) -> None:
        """Check the key exists and the configured model is in the catalog.

        The catalog is public, so this proves the model exists, not that the
        key is valid. A bad key surfaces on the first real call, exactly like
        Claude's login check: Harvey then pauses with the provider's message.
        """
        self._key()
        models = await self.list_models()
        if self.model not in models:
            suggestions = get_close_matches(self.model, models, n=3, cutoff=0.4)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise OpenCodeError(
                f"OpenCode Go model '{self.model}' is not in the Go catalog.{hint} "
                "Run 'harvey setup' or choose a model from "
                "https://opencode.ai/docs/go/.",
                kind="setup",
            )

    # ── one call ──

    async def run(self, prompt: str, session_id: str = "",
                  timeout: float | None = None) -> OpenCodeResult:
        timeout = self.timeout if timeout is None else timeout
        style = model_style(self.model)
        url = self._url(style)
        payload = self._payload(style, prompt)
        headers = self._headers(session_id)
        started = time.monotonic()

        try:
            async with self._client(timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise OpenCodeError("OpenCode Go call timed out.",
                                kind="timeout", retryable=True)
        except httpx.HTTPError as e:
            raise OpenCodeError(
                f"OpenCode Go request failed ({type(e).__name__}).",
                kind="transport", retryable=True,
            )
        duration_ms = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            raise _classify_error(response)
        try:
            body = response.json()
        except ValueError:
            raise OpenCodeError("OpenCode Go returned a non-JSON reply.",
                                kind="protocol", retryable=True)
        if not isinstance(body, dict):
            raise OpenCodeError("OpenCode Go returned an unexpected reply.",
                                kind="protocol", retryable=True)
        if isinstance(body.get("error"), dict):
            message = str(body["error"].get("message") or "OpenCode Go returned an error.")
            raise OpenCodeError(message[:500], kind="protocol")

        text = _parse_text(style, body)
        if not text:
            detail = _empty_reply_detail(style, body)
            raise OpenCodeError(
                "OpenCode Go returned no text" + (f" ({detail})" if detail else "") + ".",
                kind="protocol", retryable=True,
            )
        usage = _parse_usage(style, body, self.model)
        model = str(body.get("model") or self.model)
        request_id = str(
            body.get("id") or body.get("responseId")
            or f"opencode-{time.time_ns()}")
        return OpenCodeResult(
            text=text,
            model=model,
            request_id=request_id,
            duration_ms=duration_ms,
            usage=usage,
            cost_usd=float(usage.get("cost_usd") or 0.0),
            billing_source=str(usage.get("billing_source") or ""),
        )

    def _url(self, style: str) -> str:
        if style == "anthropic":
            return f"{self.base_url}/anthropic/v1/messages"
        if style == "responses":
            return f"{self.base_url}/openai/v1/responses"
        if style == "google":
            return (f"{self.base_url}/google/v1beta/models/"
                    f"{self.model}:generateContent")
        return f"{self.base_url}/openai/v1/chat/completions"

    def _payload(self, style: str, prompt: str) -> dict:
        if style == "anthropic":
            return {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        if style == "responses":
            return {
                "model": self.model,
                "input": prompt,
                "max_output_tokens": self.max_tokens,
            }
        if style == "google":
            return {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": self.max_tokens},
            }
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "stream": False,
        }


# ── response parsing ──


def _parse_text(style: str, body: dict) -> str:
    if style == "anthropic":
        content = body.get("content")
        if isinstance(content, list):
            return "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type", "text") == "text"
            ).strip()
        return ""
    if style == "responses":
        texts = []
        output = body.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                        texts.append(str(part.get("text") or ""))
        if not texts and isinstance(body.get("output_text"), str):
            texts.append(body["output_text"])
        return "".join(texts).strip()
    if style == "google":
        candidates = body.get("candidates")
        if isinstance(candidates, list) and candidates:
            content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if isinstance(parts, list):
                return "".join(
                    str(part.get("text") or "")
                    for part in parts if isinstance(part, dict)
                ).strip()
        return ""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "output_text")
        ).strip()
    return ""


def _empty_reply_detail(style: str, body: dict) -> str:
    if style == "google":
        feedback = body.get("promptFeedback")
        if isinstance(feedback, dict):
            return str(feedback.get("blockReason") or "")
    if style == "responses":
        detail = body.get("incomplete_details")
        if isinstance(detail, dict):
            return str(detail.get("reason") or "")
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def _parse_usage(style: str, body: dict, model: str = "") -> dict:
    raw = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    if style == "google":
        meta = body.get("usageMetadata")
        if isinstance(meta, dict):
            # Google keeps token counts in usageMetadata; keep any opencode
            # allowance block the gateway added under usage as well.
            raw = {**meta, **raw}
    opencode = raw.get("opencode") if isinstance(raw.get("opencode"), dict) else {}
    tokens = opencode.get("tokens") if isinstance(opencode.get("tokens"), dict) else {}

    if tokens:
        input_tokens = _int(tokens.get("input"))
        output_tokens = _int(tokens.get("output"))
        cache_read = _int(tokens.get("cache_read"))
        cache_creation = _int(tokens.get("cache_write_5m")) + _int(tokens.get("cache_write_1h"))
    elif style == "anthropic":
        input_tokens = _int(raw.get("input_tokens"))
        output_tokens = _int(raw.get("output_tokens"))
        cache_read = _int(raw.get("cache_read_input_tokens"))
        cache_creation = _int(raw.get("cache_creation_input_tokens"))
    elif style == "responses":
        details = raw.get("input_tokens_details") if isinstance(raw.get("input_tokens_details"), dict) else {}
        cached = _int(details.get("cached_tokens"))
        cache_creation = _int(details.get("cache_write_tokens"))
        input_tokens = max(_int(raw.get("input_tokens")) - cached, 0)
        output_tokens = _int(raw.get("output_tokens"))
        cache_read = cached
    elif style == "google":
        cached = _int(raw.get("cachedContentTokenCount"))
        input_tokens = max(_int(raw.get("promptTokenCount")) - cached, 0)
        output_tokens = _int(raw.get("candidatesTokenCount"))
        cache_read = cached
        cache_creation = 0
    else:
        details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
        cached = _int(details.get("cached_tokens"))
        input_tokens = max(_int(raw.get("prompt_tokens")) - cached, 0)
        output_tokens = _int(raw.get("completion_tokens"))
        cache_read = cached
        cache_creation = _int(details.get("cache_write_tokens"))

    allowance = _number(opencode.get("allowance"))
    list_cost = _number(opencode.get("list_cost"))
    billed_cost = _number(raw.get("cost"))
    billing_source = str(opencode.get("billing_source") or "")
    if billing_source == "go" and allowance is not None:
        cost_usd = allowance
    elif billed_cost is not None and billed_cost > 0:
        cost_usd = billed_cost
    elif list_cost is not None:
        cost_usd = list_cost
    else:
        cost_usd = estimate_cost(
            model or str(body.get("model") or ""),
            input_tokens, output_tokens, cache_read)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "cost_usd": round(float(cost_usd), 8),
        "billing_source": billing_source,
        "allowance_usd": round(float(allowance or 0.0), 8),
    }


def _error_details(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return (str(error.get("message") or ""),
                    str(error.get("type") or ""))
        if isinstance(error, str):
            return error, ""
        message = body.get("message")
        if isinstance(message, str):
            return message, str(body.get("type") or "")
    return (response.text or "").strip()[:500], ""


def _classify_error(response: httpx.Response) -> OpenCodeError:
    status = response.status_code
    message, error_type = _error_details(response)
    lower = f"{error_type} {message}".lower()
    message = message[:500] or f"OpenCode Go returned HTTP {status}."

    if status == 400 and "missingsessionid" in lower:
        return OpenCodeError(
            "OpenCode Go rejected the call because x-opencode-session was missing. "
            "This is a Harvey bug, not a key or model problem.",
            kind="isolation",
        )
    if status == 400 and "model" in lower and (
            "unavailable" in lower or "not found" in lower or "unknown" in lower):
        return OpenCodeError(message, kind="setup")
    if status == 401:
        return OpenCodeError(
            f"OpenCode Go rejected the API key. Check {API_KEY_ENV} in .env.",
            kind="auth",
        )
    if status == 403:
        if any(word in lower for word in ("quota", "limit", "usage", "allowance")):
            return OpenCodeError(message, kind="usage_limit")
        return OpenCodeError(
            f"OpenCode Go refused the request. Check {API_KEY_ENV} and your Go plan.",
            kind="auth",
        )
    if status == 402:
        return OpenCodeError(message, kind="usage_limit")
    if status in (404, 410):
        return OpenCodeError(message, kind="setup")
    if status == 429:
        retry_after = _retry_after(response)
        return OpenCodeError(
            message or "OpenCode Go usage limit reached for this plan.",
            kind="usage_limit", retry_after=retry_after,
        )
    if status >= 500:
        return OpenCodeError(message, kind="upstream", retryable=True)
    return OpenCodeError(message, kind="request")


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "").strip()
    try:
        return max(float(value), 0.0)
    except ValueError:
        return 0.0


class OpenCodeQuotaClient:
    """Go allowance windows computed from Harvey's own usage ledger.

    Go has no public usage API, but every call reports the allowance it
    consumed. Summing those rows gives the 5-hour, weekly and monthly
    utilization the plan meters, without reading the Console.
    """

    def __init__(self, state, model: str = "", monthly_limit_usd: float = 0.0):
        self.state = state
        self.model = (model or DEFAULT_MODEL).strip()
        # Go Plus doubles each model's monthly allowance; a manual override
        # keeps the gauge honest there (0 means use the model table).
        self.monthly_limit_usd = float(monthly_limit_usd or 0.0)

    async def get_utilization(self) -> dict | None:
        limit = self.monthly_limit_usd or monthly_limit(self.model)
        if not limit:
            return None
        now = datetime.now(timezone.utc)

        def stamp(dt: datetime) -> str:
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        def reset_at(oldest: str, window: timedelta, fallback: datetime) -> str:
            start = fallback
            if oldest:
                try:
                    start = datetime.strptime(oldest[:19], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    pass
            return (start + window).replace(microsecond=0).isoformat()

        windows = {}
        for key, window, share in (
            ("five_hour", timedelta(hours=5), 0.20),
            ("seven_day", timedelta(days=7), 0.50),
        ):
            spend, oldest = await self.state.usage_spend(
                "opencode", self.model, stamp(now - window))
            windows[key] = {
                "utilization": round(spend / (limit * share) * 100, 1),
                "resets_at": reset_at(oldest, window, now),
            }

        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spend, _ = await self.state.usage_spend(
            "opencode", self.model, stamp(month_start))
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        windows["monthly"] = {
            "utilization": round(spend / limit * 100, 1),
            "resets_at": next_month.isoformat(),
        }
        return windows

    def invalidate(self) -> None:
        """Kept for parity with the other quota clients; nothing is cached."""
