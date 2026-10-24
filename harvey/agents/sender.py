"""Sender — deploys campaigns via Instantly, or sends natively via the
configured mail provider (Gmail / SMTP) through the outbox approval ladder.

Native flow:
  1. Draft campaigns are STAGED: merge variables rendered per prospect,
     one outbox row per (prospect, step) with a scheduled send_at.
     Rows start as pending_review (copilot) or approved (autopilot).
  2. Each heartbeat DRAINS due approved rows: kill-switch check, stop-on-
     reply check, deterministic pre-send gate, then provider.send_email
     with jittered pacing and the daily cap.
"""

import asyncio
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone

from harvey.brain import Brain
from harvey.config import (
    HarveyConfig, EnvConfig, cold_daily_cap, compliance_footer_lines, compliance_notice,
    compliance_of,
)
from harvey.gate import autopilot_review, pre_send_check
from harvey.integrations.instantly import InstantlyClient
from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
from harvey.integrations.precleared import PreclearedClient
from harvey.sending_mode import sync_sending_mode
from harvey.state import (
    AUTOPILOT_OFF_REASON, COLLEAGUE_REPLIED, HAND_SENT_REASON, HELD_FOR_REVIEW,
    MANUAL_SEND_KEY, MAYBE_SENT_PREFIX, StateManager,
)

# A follow-up waits at least this long after the email it follows went out,
# whatever its schedule says.
MIN_FOLLOW_UP_GAP = timedelta(days=1)

logger = logging.getLogger("harvey.sender")

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
SEND_JITTER_SECONDS = (4, 15)

# Prospect pipeline statuses that mean "stop emailing this person".
STOP_STATUSES = {"replied", "opted_out", "lost", "meeting", "closed"}

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Only prospects in these statuses may be added as leads. This guarantees
# we never re-send to someone already contacted, replied, opted out, or lost.
SENDABLE_STATUSES = {"new", "queued"}

# Email confidence levels safe to send to. "verified" only by default;
# "risky" (catch-all domains) is opt-in via config. Never send to a "guess"
# or "invalid" address — that bounces and burns the sending domain.
SENDABLE_EMAIL_STATUSES = {"verified"}
SENDABLE_EMAIL_STATUSES_WITH_RISKY = {"verified", "risky"}


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
        self.provider = get_mail_provider(config, env)
        # Optional per-recipient compliance check; None unless enabled.
        self.precleared = PreclearedClient.from_config(config, env)
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def run(self):
        """Deploy campaigns — natively through the outbox, or via Instantly."""
        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        if self.is_native:
            await self._run_native()
            return

        logger.info("Sender: Checking for campaigns to deploy...")
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

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    def _reviewed_leads(self, prospects: list) -> list:
        """The prospects whose merge fields pass the autopilot review."""
        kept = []
        for p in prospects:
            fields = " \n".join(str(v or "") for v in (
                p.first_name, p.last_name, p.company, p.title, p.personalization_notes))
            held = autopilot_review("", fields, self.config, recipient=p.email)
            if held:
                logger.warning(f"Sender: left {p.email} out of the Instantly upload: {held}")
            else:
                kept.append(p)
        return kept

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly. Idempotent: safe to retry."""
        # Instantly owns campaign scheduling and has no per-message outbox
        # approval here. Fail closed rather than silently bypass the setting,
        # including retries of an already-created remote campaign.
        await sync_sending_mode(self.config, self.state)
        if getattr(self.config.channels.email, "require_approval", True):
            logger.warning(
                "Sender: Instantly campaign held because require_approval is true. "
                "Use a native mail provider for outbox review, or explicitly set "
                "channels.email.require_approval: false for Instantly autopilot."
            )
            return
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 0. Validate before touching the network
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return
        # Instantly only deploys on autopilot, so nobody has read these
        # templates: what a planted instruction would add holds the campaign.
        held = [f"step {s.step} {r}" for s in campaign.sequence
                for r in autopilot_review(s.subject, s.body, self.config)]
        if held:
            logger.warning(f"Sender: Instantly campaign '{campaign.name}' held for review: {held}")
            key = "instantly_review_hold:" + campaign.id
            if not await self.state.get_setting(key):
                await self.state.set_setting(key, "1")
                await self.state.log_action(
                    action_type="escalation", agent="sender",
                    details={"campaign_name": campaign.name,
                             "reason": "campaign held: " + "; ".join(held)[:200]},
                )
            return

        # 1. Create campaign in Instantly — or resume one from a previous
        # partially-failed deploy. Never create a duplicate.
        campaign_id = campaign.instantly_campaign_id
        if campaign_id:
            logger.info(
                f"Sender: Campaign '{campaign.name}' already has Instantly ID "
                f"{campaign_id}. Resuming deploy instead of recreating."
            )
        else:
            instantly_campaign = await self.instantly.create_campaign(campaign.name)
            if not instantly_campaign or not isinstance(instantly_campaign, dict):
                logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
                return

            campaign_id = instantly_campaign.get("id")
            if not campaign_id:
                logger.error("Sender: No campaign ID returned from Instantly.")
                return

            # Persist the ID immediately so a crash mid-deploy resumes this
            # campaign instead of creating a second one (double-send guard).
            await self.state.update_campaign(campaign.id, instantly_campaign_id=campaign_id)
            campaign.instantly_campaign_id = campaign_id

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject.strip(),
                "body": self._with_footer(step.body.strip()),
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        await self.state.remember_footer_lines(compliance_footer_lines(self.config))
        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads — validated, deduped, never-contacted, deliverable-only
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )
        prospects = []
        seen_emails: set[str] = set()
        skipped_unverified = 0
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                # Already contacted / replied / opted out — never double-send.
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            if await self.state.get_setting(MANUAL_SEND_KEY + prospect.id):
                continue  # the user emailed them by hand
            # Deliverability gate: never send to unverified/guessed addresses —
            # bounces are the fastest way to torch a sending domain.
            if (prospect.email_status or "guess") not in sendable_email:
                skipped_unverified += 1
                logger.debug(
                    f"Sender: Skipping {email} (email_status "
                    f"'{prospect.email_status or 'guess'}' not deliverable)."
                )
                continue
            seen_emails.add(email)
            prospects.append(prospect)

        if skipped_unverified:
            logger.info(
                f"Sender: Held back {skipped_unverified} prospect(s) with "
                f"unverified emails from '{campaign.name}'."
            )

        if not prospects:
            # Retry path: leads were already staged and marked 'contacted'
            # on a previous cycle but activation failed. Just activate.
            already_staged = False
            for pid in campaign.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p and p.status == "contacted":
                    already_staged = True
                    break
            if already_staged:
                logger.info(
                    f"Sender: Leads for '{campaign.name}' already staged. Retrying activation."
                )
                if await self.instantly.activate_campaign(campaign_id) is not None:
                    await self.state.update_campaign(campaign.id, status="active")
                    logger.info(f"Sender: Campaign '{campaign.name}' activated on retry.")
                return
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

        # Instantly fills these into the templates on its side, unseen:
        # scraped text carrying a planted link keeps that lead out.
        prospects = self._reviewed_leads(prospects)
        if not prospects:
            return
        leads = [
            {
                "email": p.email.strip().lower(),
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

        # Mark prospects as contacted BEFORE activation: if activation
        # succeeds but this write failed, a retry would re-add the same
        # leads. Better to under-count than double-send.
        for prospect in prospects:
            await self.state.update_prospect_status(prospect.id, "contacted")

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(
                f"Sender: Failed to activate campaign {campaign_id}. "
                "Leads are staged; will retry activation next cycle."
            )
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

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

    # ── Native provider flow (Gmail / SMTP via the outbox) ──

    async def _run_native(self):
        """Stage new campaigns into the outbox, then drain what's due."""
        # Before any early return: a send cut off mid-way must show up as
        # failed even while sending is paused or no mailbox is connected.
        stale = await self.state.fail_stale_sending_outbox()
        if stale:
            logger.warning(
                f"Sender: {stale} email(s) were interrupted mid-send and won't be "
                "retried. Check your Sent folder; they're under Didn't send."
            )
        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(
                f"Sender: SENDING PAUSED ({paused}). Resume from the dashboard "
                "or `harvey sending resume` once the cause is fixed."
            )
            return

        for campaign in await self.state.get_campaigns_by_status("draft"):
            try:
                await self._stage_campaign_native(campaign)
            except Exception as e:
                logger.error(f"Sender: staging '{campaign.name}' failed: {e}")

        await self._drain_due()

    def _render(self, text: str, prospect) -> str:
        """Fill merge variables. The pre-send gate rejects any leftovers."""
        replacements = {
            # A company inbox has no name; "there" keeps "Hi {{first_name}}," readable.
            "first_name": prospect.first_name or ("there" if prospect.is_company_inbox else ""),
            "last_name": prospect.last_name,
            "company": prospect.company,
            "title": prospect.title,
            "personalization": prospect.personalization_notes,
        }
        for key, value in replacements.items():
            text = text.replace("{{" + key + "}}", value or "")
            text = text.replace("{{ " + key + " }}", value or "")
        return text.strip()

    async def _stage_campaign_native(self, campaign):
        """Render + schedule a draft campaign's emails into the outbox."""
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        initial_status = "pending_review" if require_approval else "approved"
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )

        staged = 0
        skipped = 0
        answered = 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        seen_emails: set[str] = set()

        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email) or email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                continue
            if (prospect.email_status or "guess") not in sendable_email:
                skipped += 1
                continue
            if await self.state.colleague_who_replied(prospect.id):
                answered += 1     # their company already answered
                continue
            seen_emails.add(email)

            cumulative_days = 0
            for step in campaign.sequence:
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id,
                    campaign_id=campaign.id,
                    step=step.step,
                    to_email=email,
                    subject=self._render(step.subject, prospect),
                    body=self._render(step.body, prospect),
                    send_at=send_at.isoformat(),
                    status=initial_status,
                    provider=self.provider.name,
                    approved_by="" if require_approval else "autopilot",
                )
                if item_id:
                    staged += 1
            # Conditional: never overwrite a status set while staging
            # (marked sent by hand, a reply).
            await self.state.queue_prospect_if_unsent(prospect.id)

        await self.state.update_campaign(campaign.id, status="active")
        if skipped:
            logger.info(
                f"Sender: held back {skipped} prospect(s) with unverified "
                f"emails from '{campaign.name}'."
            )
        if answered:
            logger.info(
                f"Sender: left out {answered} prospect(s) from '{campaign.name}': "
                f"{COLLEAGUE_REPLIED} at their company."
            )
        if staged:
            mode = "awaiting your approval" if require_approval else "approved"
            logger.info(
                f"Sender: staged {staged} email(s) for '{campaign.name}' "
                f"({mode}). Review in the dashboard Outbox tab."
            )
            await self.state.log_action(
                action_type="stage_campaign",
                agent="sender",
                details={"campaign": campaign.name, "staged": staged, "mode": mode},
            )

    def _with_footer(self, body: str) -> str:
        """A cold email as it leaves: the postal address and opt-out line
        underneath (CAN-SPAM). Added at send time, so a changed address
        applies to mail already queued; not added again when the body already
        ends with it."""
        footer = "\n".join(compliance_footer_lines(self.config))
        if not footer or body.rstrip().endswith(footer):
            return body
        return body.rstrip() + "\n\n" + footer

    def _outgoing_body(self, item: dict) -> str:
        """The exact body that goes out: cold email gets the footer, replies don't."""
        if item["kind"] == "sequence":
            return self._with_footer(item["body"])
        return item["body"]

    async def _follow_up_order(self, item: dict) -> str | tuple[str, str] | None:
        """For a follow-up: "wait" until the email before it went out and its
        gap has passed, (status, error) when that email never will, else None.

        Without this, a step held for any reason (approval, a pause, a
        compliance hold, the warmup ramp) lets its follow-ups' scheduled
        times arrive first: a late "Approve all" sent the whole sequence in
        one go, or step 2 before step 1.
        """
        if item["kind"] != "sequence" or int(item["step"]) <= 1:
            return None
        prev = await self.state.get_previous_step(item)
        if prev is None:
            return None
        if prev["status"] in ("pending_review", "approved", "sending"):
            return "wait"
        if prev["status"] != "sent":
            return "cancelled", f"step {prev['step']} wasn't sent ({prev['status']})"

        def _at(value):
            try:
                return datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return None
        sent_at = _at(prev["sent_at"])
        if sent_at is None:
            return None
        delay = await self.state.get_step_delay_days(item.get("campaign_id"), item["step"])
        if delay is None:  # campaign unknown: fall back to the planned spacing
            planned, prev_planned = _at(item["send_at"]), _at(prev["send_at"])
            gap = (planned - prev_planned) if planned and prev_planned else MIN_FOLLOW_UP_GAP
        else:
            gap = timedelta(days=delay)
        ready_at = sent_at + max(MIN_FOLLOW_UP_GAP, gap)
        if datetime.now(timezone.utc).replace(tzinfo=None) >= ready_at:
            return None
        # Reschedule it for then, so it leaves the due queue until it's ready.
        await self.state.update_outbox_item(item["id"], send_at=ready_at.isoformat())
        return "wait"

    async def _blocked(self, item: dict, prospect, allow_risky: bool) -> tuple[str, str] | None:
        """Why this email must not go out, as (status, error), or None."""
        await sync_sending_mode(self.config, self.state)   # "Turn off autopilot" counts at once
        if prospect is None:
            return "failed", "prospect missing"
        if item["kind"] == "sequence":
            # Stop-on-reply / opt-out: never continue a sequence after contact.
            if prospect.status in STOP_STATUSES:
                return "cancelled", f"prospect status '{prospect.status}'"
            # The user already emailed them by hand: whatever Harvey staged
            # (before, during or after that) would be a second email.
            if await self.state.get_setting(MANUAL_SEND_KEY + prospect.id):
                return "cancelled", HAND_SENT_REASON
            # A first email to someone already contacted is always a duplicate.
            if item["step"] == 1 and prospect.status == "contacted":
                return "cancelled", "already contacted"
            # Someone else at their company answered: the rest of the
            # company gets no more cold email.
            colleague = await self.state.colleague_who_replied(prospect.id)
            if colleague:
                return "cancelled", f"{COLLEAGUE_REPLIED} ({colleague})"
        gate = pre_send_check(
            item["to_email"], item["subject"], self._outgoing_body(item),
            prospect=prospect, allow_risky=allow_risky, kind=item["kind"],
            postal_address=compliance_of(self.config).postal_address,
        )
        if not gate:
            logger.warning(
                f"Sender: gate blocked email to {item['to_email']}: "
                f"{gate.reasons}"
            )
            return "failed", "gate: " + "; ".join(gate.reasons)[:300]
        if (item.get("approved_by") == "autopilot"
                and getattr(self.config.channels.email, "require_approval", True)):
            # Autopilot approved it, and autopilot has since been turned off.
            return "pending_review", AUTOPILOT_OFF_REASON
        if item.get("approved_by") != "you":
            # Nobody approved this one (autopilot): what a planted
            # instruction would add sends it back to you instead.
            held = autopilot_review(item["subject"], self._outgoing_body(item), self.config,
                                    recipient=item["to_email"])
            if held:
                logger.warning(f"Sender: holding email to {item['to_email']} for review: {held}")
                return "pending_review", (HELD_FOR_REVIEW + "; ".join(held))[:300]
        return None

    async def _drain_due(self):
        """Send due, approved outbox items through the provider."""
        if not self.provider.is_configured():
            logger.warning(
                f"Sender: mail provider '{self.provider.name}' is not configured "
                "yet — outbox is holding. See CLAUDE.md → provider setup."
            )
            return

        max_daily = self.config.channels.email.max_daily_sends
        sent_today = await self.state.count_outbox_sent_today()
        budget = min(MAX_SENDS_PER_CYCLE, max_daily - sent_today)
        if budget <= 0:
            logger.info(f"Sender: daily send cap reached ({sent_today}/{max_daily}).")
            return
        # Cold email has its own cap under the warmup ramp; replies don't.
        # Counted from local midnight, the day the ramp counts in.
        cold_cap = cold_daily_cap(self.config.channels.email)
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        cold_left = cold_cap - await self.state.count_outbox_sent_today(
            kind="sequence",
            since=midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat())

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            skip_unready_follow_ups=True,
        )
        # Cold emails held (by an unavailable compliance check, the cold
        # cap, a follow-up's spacing) stay approved and could fill the
        # page above, so replies (and any other kind) are fetched on
        # their own too and never wait behind them. Bounded: one extra
        # query of the same size.
        seen = {item["id"] for item in due}
        unchecked = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            exclude_kind="sequence",
        )
        extra = [item for item in unchecked if item["id"] not in seen]
        if extra:
            due = sorted(
                due + extra,
                key=lambda i: (i.get("send_at") or "", i.get("created_at") or ""),
            )
        if not due:
            return

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sent = 0
        sent_cold = 0
        # Set when Precleared can't be asked (allowance used up, network):
        # cold emails wait for a later cycle, replies still go out.
        compliance_paused = False
        held_cold = 0
        for item in due:
            if sent >= budget:
                break
            if item["kind"] == "sequence" and sent_cold >= cold_left:
                held_cold += 1
                continue
            needs_check = self.precleared is not None and item["kind"] == "sequence"
            if needs_check and compliance_paused:
                continue

            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue
            order = await self._follow_up_order(item)
            if order == "wait":
                continue
            if order:
                await self.state.update_outbox_item(item["id"], status=order[0], error=order[1])
                continue

            # Optional compliance check (compliance.precleared): one per
            # address, before the first cold email. Replies are never checked.
            receipt_id = ""
            if needs_check:
                clearance = await self.precleared.clear(item["to_email"], self.state)
                if clearance.held:
                    compliance_paused = True
                    logger.warning(
                        f"Sender: compliance check unavailable, holding cold "
                        f"email until a later cycle: {clearance.reason}"
                    )
                    continue
                if not clearance.allowed:
                    await self.state.update_outbox_item(
                        item["id"], status="failed",
                        error=f"precleared: {clearance.verdict}: {clearance.reason}"[:300],
                    )
                    logger.warning(
                        f"Sender: Precleared held back email to {item['to_email']}: "
                        f"{clearance.verdict} ({clearance.reason})"
                    )
                    continue
                receipt_id = clearance.receipt_id

            # Claim it. A reply, bounce, hand send or rejection may have
            # cancelled it since the queue was read; then it stays cancelled.
            if not await self.state.claim_outbox_item(item["id"]):
                continue
            # Send the email as it is now, not as the queue read it: it may
            # have been edited and approved again since.
            item = await self.state.get_outbox_item(item["id"]) or item
            # Whatever landed between the checks above and the claim (a reply
            # sets the status before it cancels) is caught here, with fresh
            # data. From now on nothing else can cancel this email.
            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue

            body = self._outgoing_body(item)
            try:
                result = await self.provider.send_email(
                    item["to_email"], item["subject"], body,
                    thread_ref=item.get("thread_ref", ""),
                    in_reply_to=item.get("in_reply_to", ""),
                )
            except Exception as e:
                # It may have gone out before the error: never retry it.
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error=f"{MAYBE_SENT_PREFIX}send error: {e}"[:300],
                )
                logger.warning(f"Sender: send to {item['to_email']} raised: {e}")
                continue
            if not result.ok:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error=result.error[:300]
                )
                continue

            sent += 1
            if item["kind"] == "sequence":
                sent_cold += 1
            now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=now_iso, body=body,
                message_id=result.message_id, thread_ref=result.thread_ref,
            )
            if prospect.status in ("new", "queued"):
                await self.state.update_prospect_status(prospect.id, "contacted")

            # A sent reply belongs in its conversation thread.
            if item["kind"] == "reply" and item.get("conversation_id"):
                try:
                    convo = await self.state.get_conversation(item["conversation_id"])
                    if convo:
                        from harvey.models.conversation import Message
                        convo.thread.append(
                            Message(sender="harvey", content=item["body"])
                        )
                        await self.state.update_conversation(
                            convo.id, thread_json=convo.thread_json()
                        )
                except Exception as e:
                    logger.debug(f"Sender: convo append failed: {e}")
            details = {
                "to": item["to_email"], "step": item["step"],
                "kind": item["kind"], "provider": self.provider.name,
            }
            if receipt_id:
                details["precleared_receipt"] = receipt_id
            await self.state.log_action(
                action_type="email_sent",
                agent="sender",
                details=details,
            )
            logger.info(
                f"Sender: sent step {item['step']} to {item['to_email']} "
                f"via {self.provider.name}."
            )
            # Human-ish pacing BETWEEN sends (not after the last, and never
            # in tests where pacing is disabled).
            if self.send_pacing and sent < budget:
                await asyncio.sleep(random.uniform(*SEND_JITTER_SECONDS))

        if held_cold:
            warmup = getattr(self.config.channels.email, "warmup", None)
            days = warmup.days_in() if warmup is not None else None
            if days is not None and cold_cap < max_daily:
                logger.info(
                    f"Sender: warmup day {days}: {held_cold} cold email(s) wait; today's "
                    f"cold cap is {cold_cap}. Replies still go out. See `harvey warmup`.")
            else:
                logger.info(f"Sender: cold email cap reached for today ({cold_cap}).")
        if sent_cold:
            await self.state.remember_footer_lines(compliance_footer_lines(self.config))
            notice = compliance_notice(self.config)
            if notice:
                logger.warning(f"Sender: {notice}")
        if sent:
            logger.info(f"Sender: {sent} email(s) sent this cycle "
                        f"({sent_today + sent}/{max_daily} today).")
