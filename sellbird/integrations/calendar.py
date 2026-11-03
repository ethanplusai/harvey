"""Calendar integration — placeholder for future implementation."""

import logging

logger = logging.getLogger("harvey.calendar")


class CalendarClient:
    """Placeholder for calendar integration (Cal.com, Calendly, etc.).

    All methods degrade gracefully (empty results / False) so callers
    can invoke them unconditionally until a real backend is wired in.
    """

    async def get_available_slots(self, date_range: str) -> list[dict]:
        """Return available meeting slots for a date range (not yet implemented)."""
        logger.info("Calendar integration not yet implemented.")
        return []

    async def book_meeting(self, prospect_email: str, slot: dict) -> bool:
        """Book a meeting with a prospect (not yet implemented)."""
        if not prospect_email or not isinstance(slot, dict):
            logger.warning("book_meeting called with invalid arguments.")
            return False
        logger.info("Calendar integration not yet implemented.")
        return False
