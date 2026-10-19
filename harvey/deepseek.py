"""DeepSeek adapter — Harvey's brain on a DeepSeek API key.

Unlike Claude (Claude Code CLI) and Codex (ChatGPT), DeepSeek has no flat
subscription a CLI can ride on: it bills per token over an OpenAI-compatible
API. Harvey calls api.deepseek.com directly with DEEPSEEK_API_KEY. No tools
are attached — Harvey only ever needs text back, and prompts carry untrusted
text (prospect replies, scraped pages), so the model gets nothing to act with.

Environment (harvey/.env):
  DEEPSEEK_API_KEY   — required
  DEEPSEEK_BASE_URL  — optional, default https://api.deepseek.com
  DEEPSEEK_MODEL     — optional, default deepseek-chat
"""

import logging
import os
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("harvey.deepseek")

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


@dataclass
class DeepSeekResult:
    text: str
    model: str = ""
    duration_ms: int = 0
    usage: dict = field(default_factory=dict)


class DeepSeekError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class DeepSeekClient:
    """One DeepSeek chat completion per call. Stateless; reads creds from env."""

    def __init__(self, model: str = "", base_url: str = "", api_key: str = ""):
        self.model = model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
        self.base_url = (
            base_url or os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def run(self, prompt: str, timeout: float = 300.0) -> DeepSeekResult:
        if not self.api_key:
            raise DeepSeekError(
                "DEEPSEEK_API_KEY is not set in .env — add it and restart Harvey.",
                retryable=False,
            )
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(url, json=payload, headers=headers)
        duration_ms = int((time.monotonic() - start) * 1000)

        if res.status_code != 200:
            # Auth/config errors are not worth retrying; rate-limit and 5xx are.
            retryable = res.status_code == 429 or res.status_code >= 500
            raise DeepSeekError(
                f"DeepSeek HTTP {res.status_code}: {res.text[:300]}",
                retryable=retryable,
            )

        try:
            data = res.json()
        except ValueError as e:
            raise DeepSeekError(f"DeepSeek returned non-JSON: {e}", retryable=False)

        choices = data.get("choices") or [{}]
        message = (choices[0].get("message") if choices else None) or {}
        text = (message.get("content") or "").strip()
        if not text:
            # deepseek-reasoner returns reasoning_content; fall back to it.
            text = (message.get("reasoning_content") or "").strip()

        usage = data.get("usage") or {}
        return DeepSeekResult(
            text=text,
            model=data.get("model") or self.model,
            duration_ms=duration_ms,
            usage={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
            },
        )
