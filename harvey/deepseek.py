"""DeepSeek as Harvey's brain (local fork, opt-in: `brain: {provider: deepseek}`).

Unlike the Claude and Codex adapters, there is no CLI or agent here to lock
down: this is one HTTPS request to DeepSeek's chat-completions API with the
prompt as a single user message and no `tools` field, so the model can only
answer with text. It can't read files, run commands or browse — which is the
isolation Harvey needs, since its prompts carry text from websites and email.

DeepSeek bills per token from a prepaid balance, so every call records its
tokens and an estimated cost, and the budget check stops Harvey at
`brain.max_daily_usd` (see Brain.is_within_budget).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import httpx

API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
MODELS = ("deepseek-flash", "deepseek-v4-pro")
# USD per 1M tokens at PEAK rates (off-peak is half): an upper bound, so the
# daily cap errs on the safe side. From api-docs.deepseek.com/quick_start/pricing,
# October 2026; prices change, so the cap is an estimate, not an invoice.
PRICES = {
    "deepseek-flash": {"hit": 0.006, "miss": 0.30, "out": 1.20},
    "deepseek-v4-pro": {"hit": 0.044, "miss": 1.32, "out": 3.96},
}
MAX_OUTPUT_TOKENS = 4000


class DeepSeekError(Exception):
    def __init__(self, message: str, kind: str = "transient"):
        super().__init__(message)
        self.kind = kind   # auth | balance | bad_request | transient


@dataclass
class DeepSeekResult:
    text: str
    model: str
    duration_ms: int
    usage: dict = field(default_factory=dict)   # record_usage_event kwargs
    request_id: str = ""


def api_key() -> str:
    from dotenv import load_dotenv
    from harvey.paths import PROJECT_ROOT
    load_dotenv(PROJECT_ROOT / ".env")
    return (os.getenv("DEEPSEEK_API_KEY") or "").strip()


def estimate_cost(model: str, usage: dict) -> float:
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
        """"" when usable, else what's missing."""
        if not api_key():
            return "DEEPSEEK_API_KEY is not set in .env."
        if self.model not in MODELS:
            return f"brain.model '{self.model}' isn't a DeepSeek model ({', '.join(MODELS)})."
        return ""

    async def complete(self, prompt: str, timeout: float) -> DeepSeekResult:
        key = api_key()
        if not key:
            raise DeepSeekError("DEEPSEEK_API_KEY is not set in .env.", kind="auth")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            # Plain answers: thinking mode would bill reasoning tokens for
            # drafting a short email. No "tools": the model can only reply.
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
            raise DeepSeekError(f"DeepSeek request failed: {type(e).__name__}") from e
        if r.status_code == 401:
            raise DeepSeekError("DeepSeek rejected the API key (401).", kind="auth")
        if r.status_code == 402:
            raise DeepSeekError("DeepSeek balance is empty (402). Top up at platform.deepseek.com.",
                                kind="balance")
        if r.status_code in (400, 422):
            raise DeepSeekError(f"DeepSeek refused the request ({r.status_code}): {r.text[:200]}",
                                kind="bad_request")
        if r.status_code >= 400:
            raise DeepSeekError(f"DeepSeek error {r.status_code}")
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("tool_calls"):
            # Can't happen without tools in the request; refuse rather than trust it.
            raise DeepSeekError("DeepSeek answered with a tool call.", kind="bad_request")
        usage = data.get("usage") or {}
        model = str(data.get("model") or self.model)
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        return DeepSeekResult(
            text=str(message.get("content") or "").strip(),
            model=model,
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
