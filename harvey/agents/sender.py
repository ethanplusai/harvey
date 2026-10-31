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
import time
from datetime import date, datetime, timedelta, timezone

from harvey.brain import Brain
from harvey.config import (
    HarveyConfig, EnvConfig, cold_daily_cap, compliance_footer_lines, compliance_notice,
    compliance_of, live_do_not_contact, operator_timezone, send_window_of,
)
from harvey.gate import autopilot_review, do_not_contact_match, pre_send_check
from harvey.send_window import in_window, next_send_time, recipient_country, recipient_zone
from harvey.integrations.instantly import InstantlyClient
from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
from harvey.integrations.precleared import PreclearedClient, clear_hold, record_hold
from harvey.integrations.signature import signature_problem
from harvey.sending_mode import sync_sending_mode
from harvey.state import (
    AUTOPILOT_OFF_REASON, COLLEAGUE_REPLIED, HAND_SENT_REASON, HELD_FOR_REVIEW,
    MANUAL_SEND_KEY, MAYBE_SENT_PREFIX, RECHECK_PREFIX, StateManager,
)

# A follow-up waits at least this long after the email it follows went out,
# whatever its schedule says.
MIN_FOLLOW_UP_GAP = timedelta(days=1)

logger = logging.getLogger("harvey.sender")

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
# How many due emails quiet hours look through for one that can go now.
QUIET_HOURS_SCAN = 200
SEND_JITTER_SECONDS = (4, 15)
# Optional Precleared check: an email whose address Precleared couldn't
# check is moved back this far, behind other email, rather than asked about
# again every cycle. Failures for this many different domains with no answer
# in between (across cycles) mean Precleared itself is in trouble, so checks
# pause. And no new check starts after this many seconds of checking in one
# cycle.
ADDRESS_RETRY_AFTER = timedelta(hours=6)
DOMAINS_FAILING_TO_PAUSE = 2
CHECK_SECONDS_PER_CYCLE = 90

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


def _utcnow() -> datetime:
    """Naive UTC now, as the outbox stores times. Tests pin it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        # Domains whose check failed since Precleared last answered. Kept
        # across cycles: one new address a cycle failing is still a pattern.
        self._failing_domains: set[str] = set()
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True
        # Set by the heartbeat in quiet hours (send windows in recipients'
        # zones): only cold email goes, inside its window; replies wait for
        # the operator's own hours, as they always have.
        self.cold_only = False
        # The last drain couldn't ask Precleared: quiet hours wake only for
        # cold email it has already cleared until a drain outside them has run.
        self.compliance_held = False

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def _send_zone(self, prospect, country: str = "", given: str = "",
                         names: tuple[str, str] = ("", "")) -> str:
        """The time zone this recipient's send window is in: their own when
        it can be told (with recipient_timezone on), otherwise the zone in
        usage.quiet_hours, as before. ``country`` is the recipient's country
        from Precleared, ``given`` the one Harvey gave the check (see
        _recipient_country) and ``names`` the company and person names it
        gave (see _recipient_names), which a cached verdict is keyed on."""
        window = send_window_of(self.config)
        if window is not None and window.recipient_timezone:
            if not country and self.precleared is not None and prospect is not None:
                # A country Precleared already found counts on every check,
                # not only on the one that asked it.
                company, name = names
                country = await self.precleared.known_country(
                    prospect.email, self.state, given, company=company, name=name)
            zone = await recipient_zone(self.state, prospect, self.config, country)
            if zone:
                return zone
        return operator_timezone(self.config)

    async def _zone_or_default(self, prospect, country: str = "", given: str = "",
                               names: tuple[str, str] = ("", "")) -> str:
        """_send_zone, but a failure to work the zone out falls back to
        usage.quiet_hours.timezone for this one email instead of stopping
        the whole drain (replies included)."""
        try:
            return await self._send_zone(prospect, country, given, names)
        except Exception as e:
            logger.warning(
                f"Sender: couldn't work out the time zone for "
                f"{getattr(prospect, 'email', '?')}, using quiet_hours.timezone: {e}")
            return operator_timezone(self.config)

    async def _recipient_country(self, prospect) -> str:
        """The recipient's country when Harvey is sure of it, for the
        compliance check (see send_window.recipient_country). A failure to
        work it out means none for this one email, which Precleared then
        works out itself, instead of stopping the drain."""
        try:
            return await recipient_country(self.state, prospect)
        except Exception as e:
            logger.warning(
                f"Sender: couldn't work out the country for "
                f"{getattr(prospect, 'email', '?')}, leaving it to Precleared: {e}")
            return ""

    async def _recipient_names(self, prospect) -> tuple[str, str]:
        """The company's name and the person's full name, for the compliance
        check: the company record's name, else the prospect's own company
        field, and the prospect's first and last name. A company inbox has no
        person, so no name. A failure to load the company record falls back
        to the prospect's own field for this one email instead of stopping
        the drain."""
        if prospect is None:
            return "", ""
        company = ""
        if getattr(prospect, "company_id", ""):
            try:
                record = await self.state.get_company(prospect.company_id)
                company = (getattr(record, "name", "") or "") if record else ""
            except Exception as e:
                logger.warning(
                    f"Sender: couldn't load the company for "
                    f"{getattr(prospect, 'email', '?')}, using the prospect's own: {e}")
        company = company.strip() or (getattr(prospect, "company", "") or "").strip()
        name = ""
        if not getattr(prospect, "is_company_inbox", False):
            first = (getattr(prospect, "first_name", "") or "").strip()
            last = (getattr(prospect, "last_name", "") or "").strip()
            name = f"{first} {last}".strip()
        return company, name

    async def _has_cached_verdict(self, item: dict) -> bool:
        """Use the send loop's recipient facts when counting held email."""
        prospect = await self.state.get_prospect(item["prospect_id"])
        country = await self._recipient_country(prospect)
        company, name = await self._recipient_names(prospect)
        return await self.precleared.cached(
            item["to_email"], self.state, country, company=company, name=name) is not None

    async def cold_due_in_window(self) -> bool:
        """Quiet hours: whether a cold email is due, inside its recipient's
        send window and free to go now (sending not paused, under the daily
        and warmup caps, and after a compliance hold only to someone
        Precleared has already cleared). Only then is it worth waking to read
        replies and send."""
        window = send_window_of(self.config)
        if window is None or not self.is_native:
            return False
        if not self.provider.is_configured() or await self.state.get_setting(KILL_SWITCH_KEY):
            return False
        email = self.config.channels.email
        if email.max_daily_sends - await self.state.count_outbox_sent_today() <= 0:
            return False
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        if cold_daily_cap(email) - await self.state.count_outbox_sent_today(
                kind="sequence",
                since=midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat()) <= 0:
            return False
        now = _utcnow()
        due_before = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        due = await self.state.get_outbox(
            status="approved", due_before=due_before,
            limit=QUIET_HOURS_SCAN, skip_unready_follow_ups=True, kind="sequence",
        )
        if self.compliance_held and self.precleared is not None:
            # Match the drain's separate follow-up scan: waiting first emails
            # must not hide a cleared follow-up and prevent the loop waking.
            seen = {item["id"] for item in due}
            follow_ups = await self.state.get_outbox(
                status="approved", due_before=due_before,
                limit=QUIET_HOURS_SCAN, skip_unready_follow_ups=True, follow_ups_only=True,
            )
            due += [item for item in follow_ups if item["id"] not in seen]
        for item in due:
            if item["kind"] != "sequence":
                continue
            prospect = await self.state.get_prospect(item["prospect_id"])
            country = await self._recipient_country(prospect) if self.precleared is not None else ""
            names = await self._recipient_names(prospect) if self.precleared is not None else ("", "")
            if self.compliance_held and self.precleared is not None:
                # New addresses wait for a drain outside quiet hours; a
                # stored verdict still lets email go, as in the drain.
                clearance = await self.precleared.cached(
                    item["to_email"], self.state, country, company=names[0], name=names[1])
                if clearance is None or not clearance.allowed:
                    continue
            if prospect is not None and in_window(
                    now, await self._zone_or_default(prospect, given=country, names=names), window):
                return True
        return False

    async def _outside_window(self, item: dict, zone: str, window) -> bool:
        """Move a cold email that isn't in its send window to the window's
        next opening. True when it was moved and must wait."""
        now = _utcnow()
        if in_window(now, zone, window):
            return False
        later = next_send_time(now, zone, window)
        await self.state.update_outbox_item(item["id"], send_at=later.isoformat())
        return True

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
            if listed := do_not_contact_match(p.email, live_do_not_contact(self.config)):
                held = [f"'{listed}' is on the do-not-contact list", *held]
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
        # "{{company}}'s" for "Parkes Builders" should read "Builders'".
        company = prospect.company or ""
        if company.endswith(("s", "S")):
            for tag in ("{{company}}", "{{ company }}"):
                text = text.replace(tag + "'s", company + "'")
                text = text.replace(tag + "’s", company + "’")
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
        now = _utcnow()
        seen_emails: set[str] = set()
        window = send_window_of(self.config)

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

            # With a send window, each email is planned for the recipient's
            # office hours; follow-ups keep their delay, then move into it.
            zone = await self._zone_or_default(prospect) if window is not None else ""
            cumulative_days = 0
            for step in campaign.sequence:
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                if window is not None:
                    send_at = next_send_time(send_at, zone, window)
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

    async def _follow_up_order(self, item: dict, prospect=None) -> str | tuple[str, str] | None:
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
        window = send_window_of(self.config)
        if window is not None and prospect is not None:
            ready_at = next_send_time(ready_at, await self._zone_or_default(prospect), window)
        if _utcnow() >= ready_at:
            return None
        # Reschedule it for then, so it leaves the due queue until it's ready.
        await self.state.update_outbox_item(item["id"], send_at=ready_at.isoformat())
        return "wait"

    async def _retry_later(self, item: dict, reason: str) -> datetime:
        """Move a cold email back by ADDRESS_RETRY_AFTER, saying why; return
        the new time. Sending it clears the note."""
        retry_at = _utcnow() + ADDRESS_RETRY_AFTER
        await self.state.update_outbox_item(
            item["id"], send_at=retry_at.isoformat(),
            error=f"{RECHECK_PREFIX}{retry_at:%Y-%m-%d %H:%M} UTC: {reason}"[:300])
        return retry_at

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
            do_not_contact=live_do_not_contact(self.config),
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
        if self.precleared is None:
            # Check switched off: cold email no longer waits on it, so a
            # hold recorded while it was on must not stay on Today.
            await clear_hold(self.state)
        if not self.provider.is_configured():
            logger.warning(
                f"Sender: mail provider '{self.provider.name}' is not configured "
                "yet — outbox is holding. See CLAUDE.md → provider setup."
            )
            return
        # Every email carries the configured logo, or none goes out: a
        # broken logo holds approved email rather than failing it.
        problem = signature_problem(self.config)
        if problem:
            logger.warning(f"Sender: email signature can't be used ({problem}) — outbox is holding.")
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
        # In quiet hours only cold email can go, so only cold email is
        # fetched: replies due ahead of it can't fill the page.
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            skip_unready_follow_ups=True, kind="sequence" if self.cold_only else None,
        )
        # Cold emails held (by an unavailable compliance check, the cold
        # cap, a follow-up's spacing) stay approved and could fill the
        # page above, so replies (and any other kind) are fetched on
        # their own too and never wait behind them. Bounded: one extra
        # query of the same size.
        seen = {item["id"] for item in due}
        unchecked = [] if self.cold_only else await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            exclude_kind="sequence",
        )
        extra = [item for item in unchecked if item["id"] not in seen]
        if self.precleared is not None:
            # First emails waiting on Precleared keep their place and could
            # fill the page too, hiding follow-ups to people already cleared,
            # which a stored verdict lets go out. Fetched on their own as well.
            seen.update(item["id"] for item in extra)
            follow_ups = await self.state.get_outbox(
                status="approved", due_before=now, limit=budget * 3,
                skip_unready_follow_ups=True, follow_ups_only=True,
            )
            extra += [item for item in follow_ups if item["id"] not in seen]
        if extra:
            due = sorted(
                due + extra,
                key=lambda i: (i.get("send_at") or "", i.get("created_at") or ""),
            )
        if not due:
            return

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        window = send_window_of(self.config)
        sent = 0
        sent_cold = 0
        outside_window = 0
        # Set when Precleared can't be asked (allowance used up, network):
        # cold emails wait for a later cycle, replies still go out.
        compliance_paused = False
        held_cold = 0
        # For the Today page and `harvey status`: what held cold email this
        # cycle, how many waited behind it, and whether a fresh call was
        # answered (which clears an earlier hold).
        hold = None
        waiting_on_check = 0
        answered = False
        # Addresses Precleared couldn't check this cycle: their other emails
        # aren't asked about again. (self._failing_domains: failures across
        # several domains pause checks.)
        failed_addresses: set[str] = set()
        check_seconds = 0.0
        for item in due:
            if sent >= budget:
                break
            if self.cold_only and item["kind"] != "sequence":
                continue  # quiet hours: replies wait for the operator's hours
            if item["kind"] == "sequence" and sent_cold >= cold_left:
                held_cold += 1
                continue
            needs_check = self.precleared is not None and item["kind"] == "sequence"

            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue
            order = await self._follow_up_order(item, prospect)
            if order == "wait":
                continue
            if order:
                await self.state.update_outbox_item(item["id"], status=order[0], error=order[1])
                continue
            # The recipient's country when Harvey is sure of it, and the
            # company's and person's names: they go with the compliance
            # check, and a cached verdict is keyed on them.
            country = await self._recipient_country(prospect) if needs_check else ""
            names = await self._recipient_names(prospect) if needs_check else ("", "")
            # Cold email waits for the recipient's send window, however
            # late it was approved; replies don't.
            if (window is not None and item["kind"] == "sequence"
                    and await self._outside_window(
                        item, await self._zone_or_default(prospect, given=country, names=names),
                        window)):
                outside_window += 1
                continue

            # Optional compliance check (compliance.precleared): one per
            # address, before the first cold email. Replies are never checked.
            receipt_id = ""
            if needs_check:
                company, name = names
                # A stored verdict still counts once checks are paused, so
                # people already cleared keep getting their follow-ups; only
                # new addresses wait for the next cycle.
                clearance = await self.precleared.cached(
                    item["to_email"], self.state, country, company=company, name=name)
                if clearance is None and (compliance_paused
                                          or check_seconds > CHECK_SECONDS_PER_CYCLE):
                    waiting_on_check += 1
                    continue
                address = (item["to_email"] or "").strip().lower()
                if clearance is None and address in failed_addresses:
                    await self._retry_later(item, "the check failed for this address earlier this cycle")
                    continue
                if clearance is None:
                    started = time.monotonic()
                    clearance = await self.precleared.check(
                        item["to_email"], self.state, country, company=company, name=name)
                    check_seconds += time.monotonic() - started
                if clearance.held and not clearance.pause_checks:
                    # Possibly about this address alone: move its email back,
                    # behind other email, and carry on with the rest. Only a
                    # first failure counts towards pausing: an address failing
                    # again says nothing new about Precleared.
                    again = (item.get("error") or "").startswith(RECHECK_PREFIX)
                    failed_addresses.add(address)
                    if not again:
                        self._failing_domains.add(address.rpartition("@")[2])
                    waiting_on_check += 1
                    retry_at = await self._retry_later(item, clearance.reason)
                    logger.warning(
                        f"Sender: Precleared couldn't check {item['to_email']}, trying "
                        f"again after {retry_at:%Y-%m-%d %H:%M} UTC: {clearance.reason}"
                    )
                    if again or len(self._failing_domains) < DOMAINS_FAILING_TO_PAUSE:
                        continue
                if clearance.held:
                    # About Precleared, not just this address: the others
                    # would likely fail too, so no more fresh checks this cycle.
                    compliance_paused = True
                    hold = clearance
                    if clearance.pause_checks:  # else counted just above
                        waiting_on_check += 1
                        if clearance.retry_address:
                            # It may still be this address: don't let it be
                            # first in line, and asked first, every cycle.
                            await self._retry_later(item, clearance.reason)
                    logger.warning(
                        f"Sender: compliance check unavailable, holding cold "
                        f"email until a later cycle: {clearance.reason}"
                    )
                    continue
                if not clearance.cached:
                    answered = True
                    self._failing_domains.clear()
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
                # Precleared found where the recipient is, and nothing else
                # had: their own window applies from now on.
                if (window is not None and window.recipient_timezone and clearance.country
                        and not prospect.timezone):
                    zone = await self._zone_or_default(prospect, clearance.country)
                    if zone and await self._outside_window(item, zone, window):
                        outside_window += 1
                        continue

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
            # A note that the check moved it back no longer applies.
            note = {"error": ""} if (item.get("error") or "").startswith(RECHECK_PREFIX) else {}
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=now_iso, body=body,
                message_id=result.message_id, thread_ref=result.thread_ref, **note,
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

        self.compliance_held = compliance_paused
        if outside_window:
            logger.info(
                f"Sender: {outside_window} cold email(s) wait for the recipient's "
                f"send window ({window.start}-{window.end} their time).")
        if self.precleared is not None:
            if hold is not None:
                # The page read above stops at a few times the cycle's
                # budget; count every cold email still waiting on the check:
                # due with no stored verdict to go on, or moved back by it.
                waiting_on_check = max(waiting_on_check, await self.state.count_due_cold_outbox(
                    now, has_cached_verdict=self._has_cached_verdict,
                    moved_back_prefix=RECHECK_PREFIX))
                await record_hold(self.state, hold, waiting_on_check)
            elif answered:
                await clear_hold(self.state)
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
