"""LinkedIn browser automation via Playwright."""

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Optional

logger = logging.getLogger("harvey.linkedin")

COOKIES_PATH = Path(__file__).parent.parent.parent / "data" / "linkedin_cookies.json"


class LinkedInAutomation:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.browser = None
        self.context = None
        self.page = None

    async def _random_delay(self, min_s: float = 2.0, max_s: float = 6.0):
        """Human-like random delay between actions."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def start(self):
        """Launch browser and set up context."""
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        self.browser = await pw.chromium.launch(headless=True)

        # Load cookies if we have them (avoids re-login)
        if COOKIES_PATH.exists():
            cookies = json.loads(COOKIES_PATH.read_text())
            self.context = await self.browser.new_context()
            await self.context.add_cookies(cookies)
            logger.info("Loaded saved LinkedIn cookies.")
        else:
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

        self.page = await self.context.new_page()

    async def stop(self):
        """Save cookies and close browser."""
        if self.context:
            cookies = await self.context.cookies()
            COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_PATH.write_text(json.dumps(cookies))
            logger.info("Saved LinkedIn cookies.")
        if self.browser:
            await self.browser.close()

    async def login(self) -> bool:
        """Log into LinkedIn."""
        if not self.page:
            await self.start()

        await self.page.goto("https://www.linkedin.com/login")
        await self._random_delay(1, 3)

        # Check if already logged in
        if "feed" in self.page.url:
            logger.info("Already logged into LinkedIn.")
            return True

        try:
            await self.page.fill("#username", self.email)
            await self._random_delay(0.5, 1.5)
            await self.page.fill("#password", self.password)
            await self._random_delay(0.5, 1.5)
            await self.page.click('button[type="submit"]')
            await self.page.wait_for_load_state("networkidle", timeout=15000)

            if "feed" in self.page.url or "mynetwork" in self.page.url:
                logger.info("Successfully logged into LinkedIn.")
                # Save cookies for future sessions
                cookies = await self.context.cookies()
                COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
                COOKIES_PATH.write_text(json.dumps(cookies))
                return True

            # Check for security challenge
            if "checkpoint" in self.page.url:
                logger.warning(
                    "LinkedIn security checkpoint detected. "
                    "Please log in manually and save cookies."
                )
                return False

            logger.warning(f"Login may have failed. Current URL: {self.page.url}")
            return False

        except Exception as e:
            logger.error(f"LinkedIn login failed: {e}")
            return False

    async def search_people(
        self,
        keywords: str = "",
        title: str = "",
        company: str = "",
        location: str = "",
        max_results: int = 25,
    ) -> list[dict]:
        """Search LinkedIn for people matching criteria.

        Returns list of dicts with: name, title, company, linkedin_url, location
        """
        if not self.page:
            await self.start()
            if not await self.login():
                return []

        # Build search URL
        search_parts = []
        if keywords:
            search_parts.append(f"keywords={keywords}")
        if title:
            search_parts.append(f"title={title}")

        search_url = (
            f"https://www.linkedin.com/search/results/people/"
            f"?{'&'.join(search_parts)}"
        )

        results = []
        page_num = 1

        while len(results) < max_results:
            url = f"{search_url}&page={page_num}"
            await self.page.goto(url)
            await self._random_delay(3, 6)
            await self.page.wait_for_load_state("networkidle", timeout=15000)

            # Extract search result cards
            cards = await self.page.query_selector_all(
                "div.entity-result__item"
            )

            if not cards:
                # Try alternate selector
                cards = await self.page.query_selector_all(
                    'li.reusable-search__result-container'
                )

            if not cards:
                logger.info(f"No results found on page {page_num}.")
                break

            for card in cards:
                if len(results) >= max_results:
                    break

                try:
                    profile = await self._extract_search_card(card)
                    if profile:
                        results.append(profile)
                except Exception as e:
                    logger.debug(f"Error extracting search card: {e}")
                    continue

            page_num += 1
            await self._random_delay(4, 8)

            # Safety: don't go beyond 5 pages
            if page_num > 5:
                break

        logger.info(f"Found {len(results)} LinkedIn profiles.")
        return results

    async def _extract_search_card(self, card) -> Optional[dict]:
        """Extract person info from a LinkedIn search result card."""
        # Get profile link
        link_el = await card.query_selector("a.app-aware-link")
        if not link_el:
            return None
        href = await link_el.get_attribute("href")
        if not href or "/in/" not in href:
            return None
        linkedin_url = href.split("?")[0]

        # Get name
        name_el = await card.query_selector("span.entity-result__title-text a span")
        name = await name_el.inner_text() if name_el else ""
        name = name.strip()

        # Get title/headline
        title_el = await card.query_selector("div.entity-result__primary-subtitle")
        title_text = await title_el.inner_text() if title_el else ""
        title_text = title_text.strip()

        # Get location
        loc_el = await card.query_selector("div.entity-result__secondary-subtitle")
        location = await loc_el.inner_text() if loc_el else ""
        location = location.strip()

        # Split name
        parts = name.split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        # Try to extract company from title (often "Title at Company")
        company = ""
        if " at " in title_text:
            company = title_text.split(" at ", 1)[1].strip()
            title_text = title_text.split(" at ", 1)[0].strip()

        return {
            "first_name": first_name,
            "last_name": last_name,
            "title": title_text,
            "company": company,
            "linkedin_url": linkedin_url,
            "location": location,
        }

    async def extract_profile(self, linkedin_url: str) -> Optional[dict]:
        """Scrape a LinkedIn profile page for detailed info."""
        if not self.page:
            await self.start()
            if not await self.login():
                return None

        await self.page.goto(linkedin_url)
        await self._random_delay(3, 5)
        await self.page.wait_for_load_state("networkidle", timeout=15000)

        try:
            # Name
            name_el = await self.page.query_selector("h1")
            name = await name_el.inner_text() if name_el else ""

            # Headline
            headline_el = await self.page.query_selector(
                "div.text-body-medium"
            )
            headline = await headline_el.inner_text() if headline_el else ""

            # Location
            loc_el = await self.page.query_selector(
                "span.text-body-small.inline"
            )
            location = await loc_el.inner_text() if loc_el else ""

            # About section
            about_el = await self.page.query_selector(
                "section.pv-about-section div.inline-show-more-text"
            )
            about = await about_el.inner_text() if about_el else ""

            parts = name.strip().split(" ", 1)

            return {
                "first_name": parts[0] if parts else "",
                "last_name": parts[1] if len(parts) > 1 else "",
                "headline": headline.strip(),
                "location": location.strip(),
                "about": about.strip(),
                "linkedin_url": linkedin_url,
            }

        except Exception as e:
            logger.error(f"Error extracting profile {linkedin_url}: {e}")
            return None
