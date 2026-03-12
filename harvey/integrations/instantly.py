"""Instantly.ai API client for cold email campaigns."""

import logging
from typing import Any

import httpx

logger = logging.getLogger("harvey.instantly")

BASE_URL = "https://api.instantly.ai/api/v2"


class InstantlyClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, endpoint: str, data: dict | None = None
    ) -> dict | list | None:
        """Make an authenticated request to the Instantly API."""
        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                method, url, headers=self.headers, json=data
            )
            if response.status_code >= 400:
                logger.error(
                    f"Instantly API error {response.status_code}: {response.text}"
                )
                return None
            if response.status_code == 204:
                return {}
            return response.json()

    # ── Campaigns ──

    async def create_campaign(self, name: str) -> dict | None:
        """Create a new campaign. Returns campaign data with id."""
        result = await self._request("POST", "/campaigns", {"name": name})
        if result:
            logger.info(f"Created campaign: {name} (id={result.get('id')})")
        return result

    async def get_campaign(self, campaign_id: str) -> dict | None:
        """Get campaign details."""
        return await self._request("GET", f"/campaigns/{campaign_id}")

    async def list_campaigns(self, limit: int = 100) -> list:
        """List all campaigns."""
        result = await self._request("GET", f"/campaigns?limit={limit}")
        return result if isinstance(result, list) else []

    async def update_campaign_schedule(
        self,
        campaign_id: str,
        schedule: dict,
    ) -> dict | None:
        """Update campaign sending schedule."""
        return await self._request(
            "PATCH", f"/campaigns/{campaign_id}", {"schedule": schedule}
        )

    async def activate_campaign(self, campaign_id: str) -> dict | None:
        """Start a campaign."""
        return await self._request(
            "POST", f"/campaigns/{campaign_id}/activate"
        )

    async def pause_campaign(self, campaign_id: str) -> dict | None:
        """Pause a campaign."""
        return await self._request(
            "POST", f"/campaigns/{campaign_id}/pause"
        )

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
        return await self._request(
            "POST",
            f"/campaigns/{campaign_id}/emails",
            {"sequences": [sequences]},
        )

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
        return await self._request(
            "POST",
            "/leads",
            {"campaign_id": campaign_id, "leads": leads},
        )

    async def get_lead(self, email: str) -> dict | None:
        """Get lead details by email."""
        return await self._request("GET", f"/leads?email={email}")

    # ── Replies / Emails ──

    async def get_campaign_emails_sent(
        self, campaign_id: str, limit: int = 100
    ) -> list:
        """Get emails sent for a campaign."""
        result = await self._request(
            "GET", f"/emails?campaign_id={campaign_id}&limit={limit}"
        )
        return result if isinstance(result, list) else []

    async def get_replies(self, campaign_id: str | None = None) -> list:
        """Get replies, optionally filtered by campaign."""
        endpoint = "/emails/replies"
        if campaign_id:
            endpoint += f"?campaign_id={campaign_id}"
        result = await self._request("GET", endpoint)
        return result if isinstance(result, list) else []

    async def send_reply(
        self,
        reply_to_uuid: str,
        body: str,
    ) -> dict | None:
        """Send a reply to a lead's email."""
        return await self._request(
            "POST",
            "/emails/reply",
            {"reply_to_uuid": reply_to_uuid, "body": body},
        )

    # ── Analytics ──

    async def get_campaign_analytics(self, campaign_id: str) -> dict | None:
        """Get campaign performance stats."""
        return await self._request(
            "GET", f"/campaigns/{campaign_id}/analytics"
        )

    # ── Account / Warmup ──

    async def list_accounts(self) -> list:
        """List connected email accounts."""
        result = await self._request("GET", "/accounts")
        return result if isinstance(result, list) else []

    async def check_warmup_status(self, account_id: str) -> dict | None:
        """Check warmup status for an email account."""
        return await self._request("GET", f"/accounts/{account_id}/warmup")
