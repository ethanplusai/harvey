"""Calendar integration — placeholder for future implementation."""

import logging

logger = logging.getLogger("harvey.calendar")


class CalendarClient:
    """Placeholder for calendar integration (Cal.com, Calendly, etc.)."""

    async def get_available_slots(self, date_range: str) -> list[dict]:
        logger.info("Calendar integration not yet implemented.")
        return []

    async def book_meeting(self, prospect_email: str, slot: dict) -> bool:
        logger.info("Calendar integration not yet implemented.")
        return False
