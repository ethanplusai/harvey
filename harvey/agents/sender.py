"""Sender — deploys campaigns via Instantly."""

import json
import logging
from datetime import date

from harvey.brain import Brain
from harvey.config import HarveyConfig, EnvConfig
from harvey.integrations.instantly import InstantlyClient
from harvey.state import StateManager

logger = logging.getLogger("harvey.sender")


class Sender:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.instantly = InstantlyClient(env.instantly_api_key)

    async def run(self):
        """Deploy draft campaigns to Instantly."""
        logger.info("Sender: Checking for campaigns to deploy...")

        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        draft_campaigns = await self.state.get_campaigns_by_status("draft")
        if not draft_campaigns:
            logger.info("Sender: No draft campaigns to deploy.")
            return

        # Enforce daily send limit
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily send limit reached ({sends_today}/{max_sends}). Skipping.")
            return

        for campaign in draft_campaigns:
            try:
                await self._deploy_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Failed to deploy campaign {campaign.name}: {e}")

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly."""
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 1. Create campaign in Instantly
        instantly_campaign = await self.instantly.create_campaign(campaign.name)
        if not instantly_campaign:
            logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
            return

        campaign_id = instantly_campaign.get("id")
        if not campaign_id:
            logger.error("Sender: No campaign ID returned from Instantly.")
            return

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject,
                "body": step.body,
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads (capped to remaining daily budget)
        prospects = []
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if prospect and prospect.email:
                prospects.append(prospect)

        if not prospects:
            logger.warning(f"Sender: No valid prospects for campaign {campaign.name}")
            return

        # Check remaining daily send budget
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily limit reached. Deferring campaign '{campaign.name}'.")
            return
        if len(prospects) > remaining:
            logger.info(f"Sender: Capping leads from {len(prospects)} to {remaining} (daily limit).")
            prospects = prospects[:remaining]

        leads = [
            {
                "email": p.email,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company_name": p.company,
                "variables": {
                    "title": p.title,
                    "personalization": p.personalization_notes,
                },
            }
            for p in prospects
        ]

        result = await self.instantly.add_leads(campaign_id, leads)
        if result is None:
            logger.error(f"Sender: Failed to add leads to campaign {campaign_id}")
            return

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(f"Sender: Failed to activate campaign {campaign_id}")
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

        # Update prospect statuses
        for prospect in prospects:
            await self.state.update_prospect_status(prospect.id, "contacted")

        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "instantly_campaign_id": campaign_id,
                "leads_added": len(leads),
            },
        )

        logger.info(
            f"Sender: Campaign '{campaign.name}' deployed to Instantly "
            f"with {len(leads)} leads. Campaign ID: {campaign_id}"
        )

    async def _count_sends_today(self) -> int:
        """Count how many prospects were contacted today."""
        import aiosqlite
        today = date.today().isoformat()
        async with aiosqlite.connect(self.state.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE status = 'contacted' AND updated_at LIKE ?",
                (f"{today}%",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
