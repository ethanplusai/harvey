"""Instantly.ai API client for cold email campaigns."""

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger("harvey.instantly")

BASE_URL = "https://api.instantly.ai/api/v2"

# Retry policy
MAX_RETRIES = 4
BACKOFF_BASE = 1.0  # seconds
BACKOFF_CAP = 30.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Explicit timeouts: connect / read / write / pool
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


class InstantlyClient:
    def __init__(self, api_key: str):
        self.api_key = api_key or ""
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _redact(self, text: str) -> str:
        """Strip the API key from anything we log or raise."""
        if self.api_key and text:
            return text.replace(self.api_key, "***REDACTED***")
        return text or ""

    @staticmethod
    def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
        """Exponential backoff with jitter; honors Retry-After on 429."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), BACKOFF_CAP) + random.uniform(0, 1)
                except ValueError:
                    pass
        base = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
        return base * random.uniform(0.5, 1.5)

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict | list | None:
        """Make an authenticated request with retries, backoff, and validation."""
        if not self.api_key:
            logger.error("Instantly API key is not configured.")
            return None

        url = f"{BASE_URL}/{endpoint.lstrip('/')}"

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await client.request(
                        method, url, headers=self.headers, json=data, params=params
                    )
                except httpx.HTTPError as e:
                    # Network/timeout errors are retryable.
                    if attempt < MAX_RETRIES:
                        delay = self._retry_delay(attempt, None)
                        logger.warning(
                            f"Instantly request error ({method} {endpoint}): "
                            f"{self._redact(str(e))}. "
                            f"Retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s."
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error(
                        f"Instantly request failed after {MAX_RETRIES} retries "
                        f"({method} {endpoint}): {self._redact(str(e))}"
                    )
                    return None

                if response.status_code in RETRYABLE_STATUS:
                    if attempt < MAX_RETRIES:
                        delay = self._retry_delay(attempt, response)
                        label = (
                            "rate limited (429)"
                            if response.status_code == 429
                            else f"server error ({response.status_code})"
                        )
                        logger.warning(
                            f"Instantly {label} on {method} {endpoint}. "
                            f"Retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s."
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error(
                        f"Instantly API error {response.status_code} on "
                        f"{method} {endpoint} after {MAX_RETRIES} retries: "
                        f"{self._redact(response.text[:500])}"
                    )
                    return None

                if response.status_code >= 400:
                    # Non-retryable client error (400, 401, 403, 404, ...)
                    logger.error(
                        f"Instantly API error {response.status_code} on "
                        f"{method} {endpoint}: {self._redact(response.text[:500])}"
                    )
                    return None

                if response.status_code == 204 or not response.content:
                    return {}

                try:
                    payload = response.json()
                except ValueError:
                    logger.error(
                        f"Instantly returned non-JSON response on {method} "
                        f"{endpoint}: {self._redact(response.text[:200])}"
                    )
                    return None

                if not isinstance(payload, (dict, list)):
                    logger.error(
                        f"Instantly returned unexpected JSON type "
                        f"({type(payload).__name__}) on {method} {endpoint}."
                    )
                    return None
                return payload

        return None

    @staticmethod
    def _extract_items(result: Any) -> list:
        """Normalize list responses: API may return a bare list or {items: [...]}. """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            items = result.get("items") or result.get("data")
            if isinstance(items, list):
                return items
        return []

    # ── Campaigns ──

    async def create_campaign(self, name: str) -> dict | None:
        """Create a new campaign. Returns campaign data with id."""
        # stop_for_company: a reply from anyone at a company stops the
        # sequence to everyone there, as on Harvey's own providers.
        result = await self._request("POST", "/campaigns",
                                     {"name": name, "stop_for_company": True})
        if isinstance(result, dict):
            logger.info(f"Created campaign: {name} (id={result.get('id')})")
            return result
        return None

    async def get_campaign(self, campaign_id: str) -> dict | None:
        """Get campaign details."""
        result = await self._request("GET", f"/campaigns/{campaign_id}")
        return result if isinstance(result, dict) else None

    async def list_campaigns(self, limit: int = 100) -> list:
        """List all campaigns."""
        result = await self._request("GET", "/campaigns", params={"limit": limit})
        return self._extract_items(result)

    async def update_campaign_schedule(
        self,
        campaign_id: str,
        schedule: dict,
    ) -> dict | None:
        """Update campaign sending schedule."""
        result = await self._request(
            "PATCH", f"/campaigns/{campaign_id}", {"schedule": schedule}
        )
        return result if isinstance(result, dict) else None

    async def activate_campaign(self, campaign_id: str) -> dict | None:
        """Start a campaign."""
        result = await self._request("POST", f"/campaigns/{campaign_id}/activate")
        return result if isinstance(result, dict) else None

    async def pause_campaign(self, campaign_id: str) -> dict | None:
        """Pause a campaign."""
        result = await self._request("POST", f"/campaigns/{campaign_id}/pause")
        return result if isinstance(result, dict) else None

    # ── Campaign Emails (Sequences) ──

    async def set_campaign_emails(
        self,
        campaign_id: str,
        sequences: list[dict],
    ) -> dict | None:
        """Set the email sequence for a campaign.

        sequences format:
        [
            {
                "subject": "Subject line",
                "body": "Email body (HTML or plain text)",
                "wait": 0  # days to wait after previous step
            },
            ...
        ]
        """
        result = await self._request(
            "POST",
            f"/campaigns/{campaign_id}/emails",
            {"sequences": [sequences]},
        )
        return result if isinstance(result, dict) else None

    # ── Leads ──

    async def add_leads(
        self,
        campaign_id: str,
        leads: list[dict],
    ) -> dict | None:
        """Add leads to a campaign.

        leads format:
        [
            {
                "email": "john@example.com",
                "first_name": "John",
                "last_name": "Doe",
                "company_name": "Acme Inc",
                "variables": {"custom_var": "value"}
            }
        ]
        """
        if not leads:
            logger.warning("add_leads called with an empty lead list; skipping.")
            return {}
        result = await self._request(
            "POST",
            "/leads",
            {"campaign_id": campaign_id, "leads": leads},
        )
        return result if isinstance(result, dict) else None

    async def get_lead(self, email: str) -> dict | None:
        """Get lead details by email."""
        result = await self._request("GET", "/leads", params={"email": email})
        return result if isinstance(result, dict) else None

    # ── Replies / Emails ──

    async def get_campaign_emails_sent(
        self, campaign_id: str, limit: int = 100
    ) -> list:
        """Get emails sent for a campaign."""
        result = await self._request(
            "GET", "/emails", params={"campaign_id": campaign_id, "limit": limit}
        )
        return self._extract_items(result)

    async def get_replies(self, campaign_id: str | None = None) -> list:
        """Get replies, optionally filtered by campaign."""
        params = {"campaign_id": campaign_id} if campaign_id else None
        result = await self._request("GET", "/emails/replies", params=params)
        return self._extract_items(result)

    async def send_reply(
        self,
        reply_to_uuid: str,
        body: str,
    ) -> dict | None:
        """Send a reply to a lead's email."""
        result = await self._request(
            "POST",
            "/emails/reply",
            {"reply_to_uuid": reply_to_uuid, "body": body},
        )
        return result if isinstance(result, dict) else None

    # ── Analytics ──

    async def get_campaign_analytics(self, campaign_id: str) -> dict | None:
        """Get campaign performance stats."""
        result = await self._request("GET", f"/campaigns/{campaign_id}/analytics")
        return result if isinstance(result, dict) else None

    # ── Account / Warmup ──

    async def list_accounts(self) -> list:
        """List connected email accounts."""
        result = await self._request("GET", "/accounts")
        return self._extract_items(result)

    async def check_warmup_status(self, account_id: str) -> dict | None:
        """Check warmup status for an email account."""
        result = await self._request("GET", f"/accounts/{account_id}/warmup")
        return result if isinstance(result, dict) else None
