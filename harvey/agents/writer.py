"""Writer — crafts personalized email sequences."""

import json
import logging

from harvey.brain import Brain
from harvey.config import HarveyConfig
from harvey.models.campaign import Campaign, EmailStep
from harvey.state import StateManager

logger = logging.getLogger("harvey.writer")


class Writer:
    def __init__(self, brain: Brain, state: StateManager, config: HarveyConfig):
        self.brain = brain
        self.state = state
        self.config = config

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Filter to only those with emails
        prospects_with_email = [p for p in new_prospects if p.email]
        if not prospects_with_email:
            logger.info("Writer: No prospects with verified emails.")
            return

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            sequence = await self._write_sequence(prospects)
            if not sequence:
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.update_prospect_status(p.id, "queued")

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        # Inject email framework skills
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{prospect_summary}

Requirements:
- Email 1: Personalized cold intro. Short (under 100 words). One clear CTA.
- Email 2: Follow-up 3 days later. Different angle, add social proof or insight.
- Email 3: Break-up email 4 days after that. Create gentle urgency, last chance.
- Use {{{{first_name}}}}, {{{{company}}}}, {{{{title}}}} as merge variables.
- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines should be short, curiosity-driven, lowercase.

Return as JSON array:
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(prompt, session_id="harvey-writer")
        if not result or not isinstance(result, list):
            return []

        try:
            return [EmailStep(**step) for step in result]
        except Exception as e:
            logger.error(f"Writer: Failed to parse sequence: {e}")
            return []

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            key = f"{industry}-outreach"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap each batch at 50 prospects (Instantly best practice)
        capped = {}
        for key, group in batches.items():
            if len(group) > 50:
                capped[key] = group[:50]
            else:
                capped[key] = group

        return capped
