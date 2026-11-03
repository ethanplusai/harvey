"""Approval or autopilot, picked up while Harvey runs.

The dashboard switch (or a hand edit) changes channels.email.require_approval
in the config file. Whatever part of Harvey notices first, the loop, the
sender, the writer or the handler, calls sync_sending_mode, so going back to
approval always brings what autopilot approved (and hasn't sent) back to the
Outbox, whoever noticed.
"""

import logging

from harvey.config import refresh_sending_mode

logger = logging.getLogger("harvey.sending_mode")


async def sync_sending_mode(config, state) -> bool:
    """Bring ``config`` in line with its file. True when the mode changed."""
    if not refresh_sending_mode(config):
        return False
    if config.channels.email.require_approval:
        back = await state.requeue_autopilot_approved()
        logger.warning("Sending mode is now APPROVAL: every email waits in the Outbox for you."
                       + (f" {back} queued by autopilot came back to the Outbox." if back else ""))
    else:
        logger.warning("Sending mode is now AUTOPILOT: Harvey sends on its own "
                       "(emails it holds still wait for you).")
    return True


SIGNATURE_HOLD_PREFIX = "email signature: "


def held_message(hold: dict, waiting: int) -> tuple[str, str]:
    """What Today and `harvey status` say while sending is held because
    replies couldn't all be read or handled: (title, detail)."""
    reason = str(hold.get("reason") or "replies couldn't all be read").strip().rstrip(".")
    low = reason.lower()
    emails = "1 email is waiting" if waiting == 1 else f"{waiting} emails are waiting"
    if low.startswith(SIGNATURE_HOLD_PREFIX):
        return ("Harvey isn't sending: your email logo can't be used",
                f"{reason[len(SIGNATURE_HOLD_PREFIX):]}. Fix the file, or clear "
                f"channels.email.signature.logo to send plain text. {emails}.")
    if low.startswith("a reply from"):
        # The inbox was read; handling one reply failed. Its text and the
        # sender's address are no guide to a fix, so match nothing in it.
        title = "Harvey isn't sending: it couldn't handle a reply"
        fix = "Harvey tries again every cycle; if this lasts, check the log."
    else:
        title = "Harvey isn't sending: it couldn't read your inbox"
        # Only the providers' own messages name a fix.
        if "harvey gmail auth" in low:
            fix = "Run `harvey gmail auth` to sign in again."
        elif "harvey microsoft auth" in low:
            fix = "Run `harvey microsoft auth` to sign in again."
        elif "refusing a partial read" in low:
            fix = ("The mailbox got more mail in the last week than Harvey reads, so it can't "
                   "be sure it saw every reply. Send from a dedicated mailbox, or archive old mail.")
        else:
            fix = "Harvey tries again every cycle; if this lasts, check the log."
    return (title,
            f"{reason}. {fix} {emails}: replies are read first, so an opt-out "
            f"stops its follow-up before anything goes. Held since "
            f"{str(hold.get('since', '?'))[:16].replace('T', ' ')} UTC.")
