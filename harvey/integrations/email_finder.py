"""Email discovery — find email addresses without paid tools."""

import asyncio
import logging
import re
from typing import Optional

import dns.resolver
import aiosmtplib

logger = logging.getLogger("harvey.email_finder")

# Cache MX records per domain to avoid repeated lookups
_mx_cache: dict[str, str] = {}


def generate_patterns(first_name: str, last_name: str, domain: str) -> list[str]:
    """Generate common email patterns for a person at a domain."""
    first = first_name.lower().strip()
    last = last_name.lower().strip()

    if not first or not last or not domain:
        return []

    # Remove non-alpha characters
    first = re.sub(r"[^a-z]", "", first)
    last = re.sub(r"[^a-z]", "", last)

    return [
        f"{first}@{domain}",
        f"{first}.{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{first[0]}{last}@{domain}",
        f"{first}{last[0]}@{domain}",
        f"{first[0]}.{last}@{domain}",
        f"{last}.{first}@{domain}",
        f"{last}@{domain}",
        f"{first}_{last}@{domain}",
        f"{first}-{last}@{domain}",
    ]


async def get_mx_host(domain: str) -> Optional[str]:
    """Get the primary MX host for a domain."""
    if domain in _mx_cache:
        return _mx_cache[domain]

    try:
        loop = asyncio.get_event_loop()
        answers = await loop.run_in_executor(
            None, lambda: dns.resolver.resolve(domain, "MX")
        )
        # Get the lowest priority MX record
        mx_host = str(sorted(answers, key=lambda x: x.preference)[0].exchange).rstrip(".")
        _mx_cache[domain] = mx_host
        return mx_host
    except Exception as e:
        logger.debug(f"MX lookup failed for {domain}: {e}")
        return None


async def verify_email_smtp(email: str) -> bool:
    """Verify an email exists via SMTP RCPT TO check.

    Note: Many servers don't support this (catch-all domains, etc.)
    so a failed check doesn't guarantee the email is invalid.
    A successful check is a strong signal though.
    """
    domain = email.split("@")[1]
    mx_host = await get_mx_host(domain)
    if not mx_host:
        return False

    try:
        smtp = aiosmtplib.SMTP(hostname=mx_host, port=25, timeout=10)
        await smtp.connect()
        await smtp.ehlo()

        # Try RCPT TO
        code, _ = await smtp.vrfy(email)
        if code == 250:
            await smtp.quit()
            return True

        # VRFY often disabled, try MAIL FROM + RCPT TO
        await smtp.mail("verify@example.com")
        code, message = await smtp.rcpt(email)
        await smtp.quit()

        # 250 = accepted, 550 = rejected
        return code == 250

    except Exception as e:
        logger.debug(f"SMTP verification failed for {email}: {e}")
        return False


async def find_email(
    first_name: str,
    last_name: str,
    domain: str,
    verify: bool = True,
) -> Optional[str]:
    """Try to find a valid email for a person at a company domain.

    1. Generate common email patterns
    2. Check MX records exist for domain
    3. Verify each pattern via SMTP
    4. Return first verified email, or best guess if verification unavailable
    """
    patterns = generate_patterns(first_name, last_name, domain)
    if not patterns:
        return None

    # First check if domain has MX records at all
    mx_host = await get_mx_host(domain)
    if not mx_host:
        logger.info(f"No MX records for {domain}. Returning best guess.")
        return patterns[0]  # first.last@ is most common

    if not verify:
        return patterns[0]

    # Try to verify each pattern
    for email in patterns:
        logger.debug(f"Checking {email}...")
        try:
            is_valid = await verify_email_smtp(email)
            if is_valid:
                logger.info(f"Verified email: {email}")
                return email
        except Exception:
            continue

        # Rate limit between checks
        await asyncio.sleep(1)

    # If no verification worked, return the most common pattern
    logger.info(f"No verified email found. Best guess: {patterns[1]}")
    return patterns[1]  # first.last@domain is the safest guess
