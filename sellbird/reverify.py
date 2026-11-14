"""The second look: re-verify addresses that are still guesses.

The Scout verifies an address when it discovers a contact — but a contact
found *before* a verifier key existed stays a guess forever. Nothing looks
at it again, so the Writer never sees it and the contact is wasted. This
stage is that second look: it walks the backlog through the same verifier
the Scout uses, so a guess becomes verified, invalid or risky and the
pipeline can use it (or drop it).

Free-tier discipline matters more than speed here. Reoon's free plan
renews 20 credits every day (up to 600 a month) and gives 100 credits
that never expire, so this stage spends a day's worth a day and never
more, however many addresses are waiting. It checks the account balance
before it spends anything, and the same budget is shared with the Scout:
both draw on the same credits.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("sellbird.reverify")

# A day's worth of verifier calls. Reoon's free plan renews 20 a day;
# the extra 20 come out of the 100 credits that never expire, so the
# backlog moves twice as fast until that stash runs out.
DAILY_BUDGET = 40

# How many addresses one pass checks. Reoon's power mode can take a
# minute per address, so a pass is short and the rest of the day waits
# for the next heartbeat.
BATCH_SIZE = 3

# Which statuses a guess can become. "guess" stays a guess (the verifier
# couldn't decide) — never a silent "verified".
WRITABLE = ("verified", "risky")


def _budget_key(day: str) -> str:
    return f"reverify_spent:{day}"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def spent_today(state) -> int:
    """How many addresses this stage has checked today."""
    try:
        return int(await state.get_setting(_budget_key(_today()), "0") or 0)
    except (TypeError, ValueError):
        return 0


async def record_spend(state, count: int) -> None:
    if count > 0:
        await state.increment_setting(_budget_key(_today()), count)


def verifier_keys(env) -> dict:
    """Which verifiers are configured right now."""
    hunter = (getattr(env, "hunter_api_key", "") if env else "").strip()
    return {
        "reoon": (getattr(env, "reoon_api_key", "") if env else "").strip(),
        "zerobounce": (getattr(env, "zerobounce_api_key", "") if env else "").strip(),
        "hunter": hunter or os.getenv("HUNTER_API_KEY", "").strip(),
    }


async def reverify_address(email: str, env) -> tuple[Optional[str], bool]:
    """Verify one address through the same path the Scout uses.

    Returns (status, catch_all). The status is one of verified / invalid /
    risky, or None when the verifier couldn't decide — a None never
    upgrades anything.
    """
    from sellbird.integrations.email_finder import (
        _verify_candidate, classify_mx, get_mx_host,
    )

    address = (email or "").strip().lower()
    if "@" not in address:
        return None, False
    domain = address.split("@")[-1]
    mx_host = await get_mx_host(domain)
    if not mx_host:
        # No MX: the domain cannot receive mail at all.
        return "invalid", False
    return await _verify_candidate(address, classify_mx(mx_host), env)


async def reverify_batch(
    state,
    env,
    limit: int = BATCH_SIZE,
    daily_budget: int = DAILY_BUDGET,
) -> dict:
    """Check a batch of guessed addresses. Returns a small report.

    Nothing is spent when no verifier is configured, when the day's budget
    is already spent, or when the account has no credits left.
    """
    keys = verifier_keys(env)
    if not any(keys.values()):
        return {"skipped": "no verifier key", "checked": 0}

    spent = await spent_today(state)
    left = daily_budget - spent
    if left <= 0:
        return {"skipped": f"daily budget spent ({spent}/{daily_budget})", "checked": 0}

    # The account's own balance is the hard limit: the Scout spends from the
    # same credits, so ours can be lower than the day's budget.
    if keys["reoon"]:
        from sellbird.integrations.email_finder import reoon_balance

        balance = await reoon_balance(keys["reoon"])
        if balance is not None:
            if not balance.get("active"):
                return {"skipped": "verifier account is not active", "checked": 0}
            available = balance.get("daily", 0) + balance.get("instant", 0)
            if available <= 0:
                return {"skipped": "no verifier credits left", "checked": 0}
            left = min(left, available)

    batch = min(limit, left)
    prospects = await state.get_guessed_prospects(limit=batch)
    if not prospects:
        return {"skipped": "no guesses left", "checked": 0}

    results = {"verified": 0, "invalid": 0, "risky": 0, "undecided": 0}
    checked = 0
    for prospect in prospects:
        status, catch_all = await reverify_address(prospect.email, env)
        checked += 1
        if status is None:
            results["undecided"] += 1
            logger.debug(f"reverify: {prospect.email} — no verdict")
            continue
        await state.update_prospect_email(prospect.id, prospect.email, status)
        results[status] = results.get(status, 0) + 1
        logger.info(
            f"reverify: {prospect.email} [{status}]"
            + (" (catch-all)" if catch_all else "")
        )

    await record_spend(state, checked)
    report = {
        "checked": checked,
        "spent_today": spent + checked,
        "daily_budget": daily_budget,
        **results,
    }
    logger.info(
        f"reverify: {checked} address(es) checked — "
        f"{results['verified']} verified, {results['risky']} risky, "
        f"{results['invalid']} invalid, {results['undecided']} undecided."
    )
    return report
