"""Handler — monitors replies and manages conversations."""

import logging
from datetime import datetime

from harvey.brain import Brain
from harvey.config import HarveyConfig, EnvConfig
from harvey.integrations.instantly import InstantlyClient
from harvey.models.conversation import Conversation, Message
from harvey.state import StateManager

logger = logging.getLogger("harvey.handler")

INTENT_LABELS = {
    "interested",
    "objection",
    "not_interested",
    "ooo",
    "wrong_person",
    "question",
}


class Handler:
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
        """Check for new replies and handle them."""
        logger.info("Handler: Checking for replies...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("handler")

        if not self.config.channels.email.enabled:
            return

        # 1. Fetch active campaigns
        active_campaigns = await self.state.get_campaigns_by_status("active")
        if not active_campaigns:
            logger.info("Handler: No active campaigns to monitor.")
            return

        total_handled = 0

        for campaign in active_campaigns:
            if not campaign.instantly_campaign_id:
                continue

            # 2. Get replies from Instantly
            replies = await self.instantly.get_replies(campaign.instantly_campaign_id)
            if not replies:
                continue

            for reply in replies:
                try:
                    await self._handle_reply(reply, campaign)
                    total_handled += 1
                except Exception as e:
                    logger.error(f"Handler: Error processing reply: {e}")

        # Also check open conversations that need follow-up
        open_convos = await self.state.get_conversations_by_status("open")
        for convo in open_convos:
            if convo.intent == "interested":
                # Check if we need to follow up
                pass  # TODO: follow-up logic

        if total_handled:
            logger.info(f"Handler: Processed {total_handled} replies.")
        else:
            logger.info("Handler: No new replies.")

    async def _handle_reply(self, reply: dict, campaign):
        """Process a single reply."""
        lead_email = reply.get("lead_email", reply.get("from_email", ""))
        reply_text = reply.get("body", reply.get("text", ""))
        reply_uuid = reply.get("uuid", reply.get("id", ""))

        if not lead_email or not reply_text:
            return

        # Dedup: skip if we already processed this reply
        if reply_uuid and await self.state.is_reply_processed(reply_uuid):
            logger.debug(f"Handler: Reply {reply_uuid} already processed. Skipping.")
            return

        logger.info(f"Handler: Reply from {lead_email}")

        # Find the prospect by email (indexed lookup)
        prospect = await self.state.get_prospect_by_email(lead_email)

        if not prospect:
            logger.warning(f"Handler: No prospect found for {lead_email}")
            return

        # Update prospect status
        await self.state.update_prospect_status(prospect.id, "replied")

        # 1. Classify intent
        intent = await self._classify_intent(reply_text, prospect)
        logger.info(f"Handler: Intent for {lead_email}: {intent}")

        # 2. Get or create conversation
        existing_convos = await self.state.get_conversations_by_status("open")
        convo = next(
            (c for c in existing_convos if c.prospect_id == prospect.id), None
        )

        if not convo:
            convo = Conversation(
                id="",
                prospect_id=prospect.id,
                campaign_id=campaign.id,
                channel="email",
                thread=[
                    Message(sender="prospect", content=reply_text),
                ],
                intent=intent,
                status="open",
            )
            await self.state.add_conversation(convo)
        else:
            convo.thread.append(Message(sender="prospect", content=reply_text))
            convo.intent = intent
            await self.state.update_conversation(
                convo.id,
                thread_json=convo.thread_json(),
                intent=intent,
            )

        # 3. Generate and send response based on intent
        if intent == "not_interested":
            # Graceful exit — no response needed, mark as closed
            await self.state.update_conversation(convo.id, status="closed")
            await self.state.update_prospect_status(prospect.id, "lost")
            logger.info(f"Handler: {lead_email} not interested. Closing.")
            return

        if intent == "ooo":
            # Don't respond, let the sequence continue later
            logger.info(f"Handler: {lead_email} is OOO. Will follow up later.")
            return

        # For interested, objection, question, wrong_person — generate a reply
        response = await self._generate_response(intent, reply_text, prospect, convo)
        if not response:
            logger.warning(f"Handler: Could not generate response for {lead_email}")
            return

        # Send via Instantly
        if reply_uuid:
            result = await self.instantly.send_reply(reply_uuid, response)
            if result is not None:
                # Add our response to conversation
                convo.thread.append(Message(sender="harvey", content=response))
                await self.state.update_conversation(
                    convo.id,
                    thread_json=convo.thread_json(),
                )
                logger.info(f"Handler: Replied to {lead_email}")

                await self.state.log_action(
                    action_type="reply",
                    agent="handler",
                    details={
                        "prospect_email": lead_email,
                        "intent": intent,
                        "response_preview": response[:100],
                    },
                )

        # Mark reply as processed to avoid double-handling
        if reply_uuid:
            await self.state.mark_reply_processed(reply_uuid)

    async def _classify_intent(self, reply_text: str, prospect) -> str:
        """Ask the brain to classify the reply's intent."""
        prompt = f"""Classify this email reply into exactly ONE category:
- "interested" — wants to learn more, open to a meeting, positive response
- "objection" — has concerns but hasn't said no (price, timing, competition)
- "not_interested" — clearly says no, unsubscribe, do not contact
- "ooo" — out of office / auto-reply
- "wrong_person" — not the right contact, suggests someone else
- "question" — asking for more information before deciding

Reply from {prospect.full_name()} ({prospect.title} at {prospect.company}):
\"\"\"{reply_text}\"\"\"

Respond with ONLY the category label, nothing else."""

        result = await self.brain.think(prompt, session_id="harvey-handler")
        intent = result.strip().strip('"').lower()

        if intent not in INTENT_LABELS:
            logger.warning(f"Unknown intent: {intent}. Defaulting to 'question'.")
            return "question"

        return intent

    async def _generate_response(
        self, intent: str, reply_text: str, prospect, convo: Conversation
    ) -> str:
        """Generate an appropriate response based on intent."""
        # Build conversation history for context
        history = "\n".join(
            f"{'Harvey' if m.sender == 'harvey' else prospect.full_name()}: {m.content}"
            for m in convo.thread[-6:]  # Last 6 messages for context
        )

        objection_context = ""
        if intent == "objection":
            # Check if we have a pre-configured response
            for trigger, response in self.config.product.objection_responses.items():
                if trigger.lower() in reply_text.lower():
                    objection_context = f"\nSuggested approach for this objection: {response}"
                    break

        prompt = self.brain.load_prompt("handler")
        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}
Product: {self.config.product.name} — {self.config.product.description}"""

        # Inject objection handling + sales methodology skills
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Conversation so far:
{history}

The prospect's intent is: {intent}
{objection_context}

Write a reply that:"""

        if intent == "interested":
            prompt += """
- Acknowledges their interest warmly
- Suggests a specific next step (brief call or meeting)
- Keeps it short (under 80 words)
- Includes a clear CTA with flexibility on timing"""
        elif intent == "objection":
            prompt += """
- Addresses the concern directly and empathetically
- Provides evidence or a reframe
- Doesn't argue — redirect toward value
- Keeps it under 100 words"""
        elif intent == "question":
            prompt += """
- Answers their question clearly and concisely
- Ties the answer back to value for them
- Ends with a soft CTA
- Under 100 words"""
        elif intent == "wrong_person":
            prompt += """
- Thanks them politely
- Asks who the right person would be
- Makes it easy for them to refer (one-line ask)
- Under 50 words"""

        prompt += "\n\nWrite ONLY the email body. No subject line, no greeting label."

        response = await self.brain.think(prompt, session_id="harvey-handler")
        return response.strip() if response else ""
