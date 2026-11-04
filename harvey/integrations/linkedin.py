"""LinkedIn browser automation via Playwright.

Safety-first: humanlike randomized delays, hard daily activity caps
(persisted across restarts), and consistent browser fingerprint to
protect the user's LinkedIn account from restriction.
"""

import asyncio
import json
import logging
import platform
import random
import re
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger("harvey.linkedin")

from harvey.paths import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"
COOKIES_PATH = DATA_DIR / "linkedin_cookies.json"
ACTIVITY_PATH = DATA_DIR / "linkedin_activity.json"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.8078.4 Safari/537.36"
)


def browser_user_agent(version: str) -> str:
    """Keep the default UA aligned with the actual browser on every start."""
    version = version.removeprefix("Chrome/")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        raise RuntimeError("Cannot determine Chromium version for LinkedIn user agent.")
    ua = re.sub(r"Chrome/[\d.]+", f"Chrome/{version}", USER_AGENT)
    system = platform.system()
    if system == "Windows":
        ua = ua.replace("X11; Linux x86_64", "Windows NT 10.0; Win64; x64")
    elif system == "Darwin":
        ua = ua.replace("X11; Linux x86_64", "Macintosh; Intel Mac OS X 10_15_7")
    return ua

# Hard daily caps — conservative on purpose. LinkedIn restricts accounts
# that view too many profiles or run too many searches per day.
MAX_DAILY_SEARCH_PAGES = 20
MAX_DAILY_PROFILE_VIEWS = 40
MAX_SEARCH_PAGES_PER_QUERY = 5


class LinkedInAutomation:
    def __init__(self, email: str, password: str, *, user_agent: str = "",
                 executable_path: str = ""):
        self.email = email
        self.password = password
        self.user_agent = user_agent.strip()
        self.executable_path = executable_path.strip()
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None
        self._activity = self._load_activity()

    # ── Daily activity caps ──

    @staticmethod
    def _load_activity() -> dict:
        """Load today's activity counters (persisted so restarts don't reset caps)."""
        today = date.today().isoformat()
        try:
            if ACTIVITY_PATH.exists():
                data = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("date") == today:
                    return {
                        "date": today,
                        "search_pages": int(data.get("search_pages", 0)),
                        "profile_views": int(data.get("profile_views", 0)),
                    }
        except (ValueError, OSError) as e:
            logger.debug(f"Could not load LinkedIn activity log: {e}")
        return {"date": today, "search_pages": 0, "profile_views": 0}

    def _save_activity(self):
        try:
            ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
            ACTIVITY_PATH.write_text(json.dumps(self._activity), encoding="utf-8")
        except OSError as e:
            logger.debug(f"Could not save LinkedIn activity log: {e}")

    def _record(self, kind: str):
        # Roll over if the date changed mid-run
        today = date.today().isoformat()
        if self._activity.get("date") != today:
            self._activity = {"date": today, "search_pages": 0, "profile_views": 0}
        self._activity[kind] = self._activity.get(kind, 0) + 1
        self._save_activity()

    def _cap_reached(self, kind: str, cap: int) -> bool:
        today = date.today().isoformat()
        if self._activity.get("date") != today:
            self._activity = {"date": today, "search_pages": 0, "profile_views": 0}
        if self._activity.get(kind, 0) >= cap:
            logger.warning(
                f"LinkedIn daily cap reached for {kind} "
                f"({cap}/day). Skipping to protect the account."
            )
            return True
        return False

    # ── Humanlike behavior ──

    async def _random_delay(self, min_s: float = 2.0, max_s: float = 6.0):
        """Human-like random delay between actions, with occasional long pauses."""
        delay = random.uniform(min_s, max_s)
        # ~10% of the time, take a noticeably longer "reading" pause
        if random.random() < 0.10:
            delay += random.uniform(4.0, 10.0)
        await asyncio.sleep(delay)

    async def _humanlike_scroll(self):
        """Scroll the page in a few small increments like a human reading."""
        if not self.page:
            return
        try:
            for _ in range(random.randint(2, 4)):
                await self.page.mouse.wheel(0, random.randint(250, 700))
                await asyncio.sleep(random.uniform(0.4, 1.4))
        except Exception as e:
            logger.debug(f"Scroll simulation failed: {e}")

    async def _wait_settled(self, timeout: int = 15000):
        """Wait for network idle without crashing on busy pages."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            # Feed-like pages never go fully idle; that's fine.
            pass

    # ── Lifecycle ──

    async def start(self):
        """Launch browser and set up context."""
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            executable = self.executable_path or self._pw.chromium.executable_path
            if not Path(executable).is_file():
                raise RuntimeError(
                    "LinkedIn full Chromium binary not found. Run "
                    "'python -m playwright install chromium' (without --only-shell) "
                    "or set channels.linkedin.executable_path to a full Chromium binary."
                )
            # channel=chromium opts into full Chromium's new headless mode;
            # do not fall back to chrome-headless-shell.
            self.browser = await self._pw.chromium.launch(
                channel="chromium", headless=True, executable_path=executable,
            )
            ua = self.user_agent or browser_user_agent(self.browser.version)
            self.context = await self.browser.new_context(
                viewport={"width": 1280, "height": 800}, user_agent=ua,
            )
        except Exception as exc:
            await self.stop()
            raise RuntimeError(
                "LinkedIn browser startup failed: full Chromium is required. "
                "Check channels.linkedin.executable_path and Playwright browser installation."
                f" {exc}"
            ) from exc

        # Load cookies if we have them (avoids re-login)
        if COOKIES_PATH.exists():
            try:
                cookies = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
                if isinstance(cookies, list):
                    await self.context.add_cookies(cookies)
                    logger.info("Loaded saved LinkedIn cookies.")
            except Exception as e:
                logger.warning(f"Could not load saved LinkedIn cookies: {e}")

        self.page = await self.context.new_page()

    def _save_cookies_sync(self, cookies: list):
        try:
            COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_PATH.write_text(json.dumps(cookies), encoding="utf-8")
            try:
                COOKIES_PATH.chmod(0o600)  # session cookies are credentials
            except OSError:
                pass
        except OSError as e:
            logger.warning(f"Could not save LinkedIn cookies: {e}")

    async def stop(self):
        """Save cookies and close browser."""
        try:
            if self.context:
                cookies = await self.context.cookies()
                self._save_cookies_sync(cookies)
                logger.info("Saved LinkedIn cookies.")
        except Exception as e:
            logger.debug(f"Could not save cookies on shutdown: {e}")
        try:
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.debug(f"Error closing browser: {e}")
        finally:
            self.browser = None
            self.context = None
            self.page = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            logger.debug(f"Error stopping Playwright: {e}")
        finally:
            self._pw = None

    # ── Auth ──

    async def _fill_first_visible(self, selectors: list, value: str) -> bool:
        """Rellena el primer elemento VISIBLE que case con los selectores."""
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
            except Exception:
                continue
            for index in range(count):
                element = locator.nth(index)
                try:
                    if not await element.is_visible():
                        continue
                    await element.fill(value)
                    return True
                except Exception:
                    continue
        return False

    async def _click_login_button(self) -> bool:
        """Pulsa el boton de envio del formulario de login."""
        wanted = {"iniciar sesion", "sign in"}
        try:
            buttons = self.page.locator("button")
            count = await buttons.count()
        except Exception:
            return False
        for index in range(count):
            button = buttons.nth(index)
            try:
                if not await button.is_visible():
                    continue
                label = " ".join((await button.inner_text() or "").split())
                if label.lower().replace("\u00f3", "o") in wanted:
                    await button.click()
                    return True
            except Exception:
                continue
        return False

    async def login(self) -> bool:
        """Log into LinkedIn."""
        if not self.page:
            await self.start()

        if not self.email or not self.password:
            logger.warning("LinkedIn credentials not configured; cannot log in.")
            return False

        try:
            await self.page.goto(
                "https://www.linkedin.com/login", timeout=30000
            )
            await self._wait_settled()
        except Exception as e:
            logger.error(f"Could not load LinkedIn login page: {e}")
            return False

        await self._random_delay(1, 3)

        # Check if already logged in (cookie session redirected us)
        if "feed" in self.page.url or "mynetwork" in self.page.url:
            logger.info("Already logged into LinkedIn.")
            return True

        try:
            filled_user = await self._fill_first_visible(
                [
                    "input[autocomplete=\"username\"]",
                    "input[name=\"session_key\"]",
                    "input[type=\"email\"]",
                    "#username",
                ],
                self.email,
            )
            if not filled_user:
                logger.warning("LinkedIn login: no se encontro el campo de usuario.")
                return False
            await self._random_delay(0.5, 1.5)

            filled_pass = await self._fill_first_visible(
                [
                    "input[autocomplete=\"current-password\"]",
                    "input[type=\"password\"]",
                    "#password",
                ],
                self.password,
            )
            if not filled_pass:
                logger.warning("LinkedIn login: no se encontro el campo de password.")
                return False
            await self._random_delay(0.5, 1.5)

            if not await self._click_login_button():
                logger.info("LinkedIn login: boton no encontrado, se envia con Enter.")
                try:
                    await self.page.locator("input[type=\"password\"]").first.press("Enter")
                except Exception:
                    return False
            await self._wait_settled()

            if "feed" in self.page.url or "mynetwork" in self.page.url:
                logger.info("Successfully logged into LinkedIn.")
                # Save cookies for future sessions
                cookies = await self.context.cookies()
                self._save_cookies_sync(cookies)
                return True

            # Check for security challenge
            if "checkpoint" in self.page.url or "challenge" in self.page.url:
                logger.warning(
                    "LinkedIn security checkpoint detected. "
                    "Please log in manually and save cookies."
                )
                return False

            # El reCAPTCHA se renderiza dentro de la pagina de login (no cambia
            # la URL), asi que hay que mirarlo explicitamente.
            try:
                iframes = self.page.locator("iframe")
                for index in range(await iframes.count()):
                    src_attr = await iframes.nth(index).get_attribute("src") or ""
                    if "recaptcha" in src_attr or "captcha" in src_attr:
                        logger.warning(
                            "LinkedIn presento un reCAPTCHA en el login: la "
                            "automatizacion esta bloqueada en este intento. "
                            "Hay que iniciar sesion manualmente y guardar las "
                            "cookies en " + str(COOKIES_PATH) + "."
                        )
                        return False
            except Exception:
                pass

            logger.warning(f"Login may have failed. Current URL: {self.page.url}")
            return False

        except Exception as e:
            # Never include credentials in logged errors.
            msg = str(e).replace(self.password, "***") if self.password else str(e)
            logger.error(f"LinkedIn login failed: {msg}")
            return False

    # ── Search ──

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

        # Fold all criteria into the keyword search (URL-encoded).
        terms = " ".join(t for t in [keywords, title, company, location] if t).strip()
        if not terms:
            logger.warning("search_people called with no criteria; skipping.")
            return []

        search_url = (
            "https://www.linkedin.com/search/results/people/"
            f"?keywords={quote_plus(terms)}"
        )

        results = []
        page_num = 1

        while len(results) < max_results:
            if self._cap_reached("search_pages", MAX_DAILY_SEARCH_PAGES):
                break

            url = f"{search_url}&page={page_num}"
            try:
                await self.page.goto(url, timeout=30000)
            except Exception as e:
                logger.warning(f"Failed to load search page {page_num}: {e}")
                break

            self._record("search_pages")
            await self._random_delay(3, 6)
            await self._wait_settled()
            await self._humanlike_scroll()

            # Extract search result cards
            cards = await self.page.query_selector_all("div.entity-result__item")

            if not cards:
                # Try alternate selector
                cards = await self.page.query_selector_all(
                    "li.reusable-search__result-container"
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

            # Safety: don't paginate deep on a single query
            if page_num > MAX_SEARCH_PAGES_PER_QUERY:
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
            title_text, company = (
                s.strip() for s in title_text.split(" at ", 1)
            )

        return {
            "first_name": first_name,
            "last_name": last_name,
            "title": title_text,
            "company": company,
            "linkedin_url": linkedin_url,
            "location": location,
        }

    # ── Profiles ──

    async def extract_profile(self, linkedin_url: str) -> Optional[dict]:
        """Scrape a LinkedIn profile page for detailed info."""
        if not self.page:
            await self.start()
            if not await self.login():
                return None

        if self._cap_reached("profile_views", MAX_DAILY_PROFILE_VIEWS):
            return None

        try:
            await self.page.goto(linkedin_url, timeout=30000)
        except Exception as e:
            logger.warning(f"Failed to load profile {linkedin_url}: {e}")
            return None

        self._record("profile_views")
        await self._random_delay(3, 5)
        await self._wait_settled()
        await self._humanlike_scroll()

        try:
            # Name
            name_el = await self.page.query_selector("h1")
            name = await name_el.inner_text() if name_el else ""

            # Headline
            headline_el = await self.page.query_selector("div.text-body-medium")
            headline = await headline_el.inner_text() if headline_el else ""

            # Location
            loc_el = await self.page.query_selector("span.text-body-small.inline")
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
