"""DeepSeek as Sellbird's brain (opt-in: ``brain: {provider: deepseek}``).

Unlike the Claude and Codex adapters there is no CLI or agent to lock down:
each call is one HTTPS request to DeepSeek's chat-completions API, with the
prompt as a single user message and no ``tools`` field, so the model can only
answer with text. It can't read files, run commands or browse, which is the
isolation Sellbird needs, since its prompts carry text from websites and email.

DeepSeek bills per token from a prepaid balance (there is no subscription
quota to read), so every call records its tokens and an estimated cost, and
the budget check stops Sellbird for the day at ``brain.max_daily_usd``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx

API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
MODELS = ("deepseek-flash", "deepseek-v4-pro")
# USD per 1M tokens at PEAK rates (off-peak is half). Using peak makes the
# daily cap an upper bound: the real bill is the same or lower. From
# api-docs.deepseek.com/quick_start/pricing; prices change, so the cap is an
# estimate, not an invoice.
PRICES = {
    "deepseek-flash": {"hit": 0.006, "miss": 0.30, "out": 1.20},
    "deepseek-v4-pro": {"hit": 0.044, "miss": 1.32, "out": 3.96},
}
# Sellbird's replies are short emails and JSON; this bounds a runaway answer.
MAX_OUTPUT_TOKENS = 4000
MISSING_KEY = "DEEPSEEK_API_KEY is not set in .env."


class DeepSeekError(Exception):
    """kind: auth | balance | bad_request | transient. Only transient is
    worth retrying; auth and balance need the operator."""

    def __init__(self, message: str, kind: str = "transient"):
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind == "transient"


@dataclass
class DeepSeekResult:
    text: str
    model: str
    duration_ms: int
    usage: dict = field(default_factory=dict)  # record_usage_event kwargs
    request_id: str = ""


def api_key() -> str:
    """The key from the checkout's .env, else the environment (never logged).
    The file is read each time, so a key fixed in .env works without a
    restart."""
    from dotenv import dotenv_values

    from sellbird.paths import PROJECT_ROOT

    try:
        from_file = dotenv_values(PROJECT_ROOT / ".env").get("DEEPSEEK_API_KEY")
    except OSError:
        from_file = None
    return (from_file or os.getenv("DEEPSEEK_API_KEY") or "").strip()


def estimate_cost(model: str, usage: dict) -> float:
    """Dollars for one call at peak rates. An unknown model is priced as the
    dearest one, so the cap never under-counts."""
    price = PRICES.get(model, PRICES["deepseek-v4-pro"])
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens")
               or max(0, int(usage.get("prompt_tokens") or 0) - hit))
    out = int(usage.get("completion_tokens") or 0)
    return (hit * price["hit"] + miss * price["miss"] + out * price["out"]) / 1_000_000


class DeepSeekClient:
    def __init__(self, model: str = ""):
        self.model = model or DEFAULT_MODEL

    def configured(self) -> str:
        """"" when usable, else what's missing. No request is made."""
        if not api_key():
            return MISSING_KEY
        if self.model not in MODELS:
            return (f"brain.model '{self.model}' isn't a DeepSeek model. "
                    f"Use one of: {', '.join(MODELS)}.")
        return ""

    async def complete(self, prompt: str, timeout: float) -> DeepSeekResult:
        key = api_key()
        if not key:
            raise DeepSeekError(MISSING_KEY, kind="auth")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            # Thinking mode is DeepSeek's default and bills reasoning tokens;
            # a short email doesn't need it. No "tools": text answers only.
            "thinking": {"type": "disabled"},
            "max_tokens": MAX_OUTPUT_TOKENS,
            "stream": False,
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(API_URL, json=body,
                                      headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as e:
            raise DeepSeekError(f"DeepSeek request failed ({type(e).__name__}).") from e
        if r.status_code == 401:
            raise DeepSeekError("DeepSeek rejected the API key (401). Check "
                                "DEEPSEEK_API_KEY in .env.", kind="auth")
        if r.status_code == 402:
            raise DeepSeekError("DeepSeek balance is empty (402). Top up at "
                                "platform.deepseek.com.", kind="balance")
        if r.status_code in (400, 422):
            raise DeepSeekError(f"DeepSeek refused the request ({r.status_code}): "
                                f"{r.text[:200]}", kind="bad_request")
        if r.status_code >= 400:
            # 429 (rate limit), 500, 503: worth another try later.
            raise DeepSeekError(f"DeepSeek error {r.status_code}.")
        try:
            data = r.json()
        except ValueError as e:
            raise DeepSeekError("DeepSeek returned a response that isn't JSON.") from e
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("tool_calls"):
            # Can't happen without tools in the request; refuse rather than trust it.
            raise DeepSeekError("DeepSeek answered with a tool call.", kind="bad_request")
        usage = data.get("usage") or {}
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        return DeepSeekResult(
            text=str(message.get("content") or "").strip(),
            model=str(data.get("model") or self.model),
            duration_ms=int((time.monotonic() - started) * 1000),
            request_id=str(data.get("id") or ""),
            usage={
                "input_tokens": max(0, int(usage.get("prompt_tokens") or 0) - hit),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "cache_read_tokens": hit,
                "cache_creation_tokens": 0,
                "cost_usd": estimate_cost(self.model, usage),
            },
        )
