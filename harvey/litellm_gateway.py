"""Text-only chat completions through an operator's LiteLLM AI Gateway.

The gateway owns provider credentials, routing and API budgets. Never log
its URL, virtual key, response body or HTTP exception text: these can carry
credentials or private deployment details.
"""

import ipaddress
import os
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from harvey.paths import PROJECT_ROOT


class LiteLLMError(Exception):
    """An actionable error safe to show in logs and the dashboard."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.kind = "gateway"


class _Message(BaseModel):
    content: str


class _Choice(BaseModel):
    message: _Message
    finish_reason: str | None = None


class _Usage(BaseModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class GatewayResult(BaseModel):
    id: str
    model: str
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage = Field(default_factory=_Usage)


class LiteLLMClient:
    def __init__(self, model: str):
        self.model = model

    def _settings(self) -> tuple[str, str]:
        load_dotenv(PROJECT_ROOT / ".env")
        base_url = (
            os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
            .strip()
            .rstrip("/")
        )
        key = os.environ.get("LITELLM_API_KEY", "").strip()
        if not key:
            raise LiteLLMError(
                "Set LITELLM_API_KEY in .env to your gateway virtual key."
            )
        try:
            url = urlsplit(base_url)
            local = url.hostname == "localhost"
            if not local:
                try:
                    local = ipaddress.ip_address(url.hostname or "").is_loopback
                except ValueError:
                    pass
            valid = url.scheme == "https" or (url.scheme == "http" and local)
            if (
                not valid
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
            ):
                raise ValueError
            _ = url.port
        except ValueError:
            raise LiteLLMError(
                "LITELLM_BASE_URL must be an HTTPS URL (HTTP is allowed on loopback), "
                "without credentials, query parameters or a fragment."
            ) from None
        return base_url, key

    async def _request(
        self, method: str, path: str, *, timeout: float, **kwargs
    ) -> httpx.Response:
        base_url, key = self._settings()
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    base_url + path,
                    headers={"Authorization": f"Bearer {key}"},
                    **kwargs,
                )
        except (httpx.TimeoutException, httpx.TransportError):
            raise LiteLLMError(
                "LiteLLM AI Gateway could not be reached. Check its URL and network.",
                retryable=True,
            ) from None
        if response.status_code in (401, 403):
            raise LiteLLMError(
                "LiteLLM AI Gateway refused access. Check your virtual key and model permissions."
            )
        if not response.is_success:
            raise LiteLLMError(
                f"LiteLLM AI Gateway returned HTTP {response.status_code}. "
                "Check the gateway's model configuration, budget and availability.",
                retryable=response.status_code in (408, 429)
                or response.status_code >= 500,
            )
        return response

    async def preflight(self) -> None:
        """Check gateway access and the configured alias without inference."""
        response = await self._request("GET", "/models", timeout=15)
        try:
            models = response.json()["data"]
            if any(model["id"] == self.model for model in models):
                return
        except (ValueError, KeyError, TypeError):
            raise LiteLLMError(
                "LiteLLM AI Gateway returned an invalid model list."
            ) from None
        raise LiteLLMError(
            "The configured brain.model is unavailable to your LiteLLM virtual key."
        )

    async def run(self, prompt: str, *, timeout: float) -> GatewayResult:
        response = await self._request(
            "POST",
            "/chat/completions",
            timeout=timeout,
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
        )
        try:
            result = GatewayResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise LiteLLMError(
                "LiteLLM AI Gateway returned an invalid text completion."
            ) from None
        choice = result.choices[0]
        if not choice.message.content.strip() or choice.finish_reason not in (
            None,
            "stop",
        ):
            raise LiteLLMError(
                "LiteLLM AI Gateway returned an empty, filtered or incomplete completion."
            )
        return result
