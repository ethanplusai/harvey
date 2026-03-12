"""Scout — DIY prospecting without expensive tools."""

import logging
import re
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from harvey.brain import Brain
from harvey.config import HarveyConfig, EnvConfig
from harvey.integrations.email_finder import find_email
from harvey.integrations.linkedin import LinkedInAutomation
from harvey.models.prospect import Prospect
from harvey.state import StateManager

logger = logging.getLogger("harvey.scout")


class Scout:
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
        self.env = env

    async def run(self):
        """Main prospecting flow: find leads matching ICP."""
        logger.info("Scout: Starting prospecting cycle...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("scout")

        prospects_found = 0

        # Strategy 1: LinkedIn search (if enabled)
        if self.config.channels.linkedin.enabled and self.env.linkedin_email:
            prospects_found += await self._prospect_via_linkedin()

        # Strategy 2: Google dorking for LinkedIn profiles
        prospects_found += await self._prospect_via_google()

        # Strategy 3: Company website scraping for team pages
        prospects_found += await self._prospect_via_company_sites()

        await self.state.log_action(
            action_type="prospect",
            agent="scout",
            details={"prospects_found": prospects_found},
        )
        logger.info(f"Scout: Found {prospects_found} new prospects this cycle.")

    async def _prospect_via_linkedin(self) -> int:
        """Search LinkedIn for ICP-matching profiles."""
        logger.info("Scout: Searching LinkedIn...")
        linkedin = LinkedInAutomation(
            self.env.linkedin_email, self.env.linkedin_password
        )

        try:
            await linkedin.start()
            if not await linkedin.login():
                logger.warning("Scout: LinkedIn login failed. Skipping.")
                return 0

            count = 0
            for title in self.config.icp.titles:
                for industry in self.config.icp.industries:
                    keywords = f"{title} {industry}"
                    profiles = await linkedin.search_people(
                        keywords=keywords,
                        max_results=10,
                    )

                    for profile in profiles:
                        # Check if we already have this prospect
                        if await self.state.prospect_exists(
                            linkedin_url=profile.get("linkedin_url", "")
                        ):
                            continue

                        # Try to find their email
                        email = ""
                        company = profile.get("company", "")
                        if company:
                            domain = await self._guess_domain(company)
                            if domain:
                                email = await find_email(
                                    profile.get("first_name", ""),
                                    profile.get("last_name", ""),
                                    domain,
                                ) or ""

                        prospect = Prospect(
                            id="",
                            first_name=profile.get("first_name", ""),
                            last_name=profile.get("last_name", ""),
                            email=email,
                            linkedin_url=profile.get("linkedin_url", ""),
                            company=company,
                            title=profile.get("title", ""),
                            industry=industry,
                            source="linkedin_search",
                        )

                        # Ask brain for personalization notes
                        if email or profile.get("linkedin_url"):
                            notes = await self._get_personalization(prospect)
                            prospect.personalization_notes = notes

                        await self.state.add_prospect(prospect)
                        count += 1
                        logger.info(
                            f"Scout: Added prospect {prospect.full_name()} "
                            f"at {company}"
                        )

            return count

        finally:
            await linkedin.stop()

    async def _prospect_via_google(self) -> int:
        """Use Google dorking to find LinkedIn profiles matching ICP."""
        logger.info("Scout: Google dorking for prospects...")
        count = 0

        for title in self.config.icp.titles:
            for geo in self.config.icp.geography:
                query = (
                    f'site:linkedin.com/in "{title}" '
                    f'"{self.config.icp.industries[0]}" "{geo}"'
                )
                profiles = await self._google_search(query)

                for url, snippet in profiles:
                    if "/in/" not in url:
                        continue
                    if await self.state.prospect_exists(linkedin_url=url):
                        continue

                    # Parse name from URL or snippet
                    parsed = self._parse_linkedin_url(url, snippet)
                    if not parsed:
                        continue

                    prospect = Prospect(
                        id="",
                        first_name=parsed.get("first_name", ""),
                        last_name=parsed.get("last_name", ""),
                        linkedin_url=url,
                        title=title,
                        source="google_dork",
                    )
                    await self.state.add_prospect(prospect)
                    count += 1

                if count >= 20:
                    break
            if count >= 20:
                break

        return count

    async def _prospect_via_company_sites(self) -> int:
        """Scrape company team/about pages for prospects."""
        logger.info("Scout: Checking company websites...")

        # Ask brain to find target companies
        prompt = self.brain.load_prompt(
            "scout",
            industries=", ".join(self.config.icp.industries),
            company_size=self.config.icp.company_size,
            geography=", ".join(self.config.icp.geography),
            titles=", ".join(self.config.icp.titles),
        )

        if not prompt:
            prompt = f"""Find 10 companies that match this ICP:
- Industries: {', '.join(self.config.icp.industries)}
- Company size: {self.config.icp.company_size}
- Geography: {', '.join(self.config.icp.geography)}

Return a JSON array of objects with "name" and "domain" fields.
Example: [{{"name": "Acme Inc", "domain": "acme.com"}}]"""

        # Inject skills knowledge
        if self.skills:
            prompt = self.skills + "\n\n---\n\n" + prompt

        result = await self.brain.think_json(prompt, session_id="harvey-scout")
        if not result or not isinstance(result, list):
            return 0

        count = 0
        for company_info in result[:10]:
            domain = company_info.get("domain", "")
            company_name = company_info.get("name", "")
            if not domain:
                continue

            # Try to find team page
            team_members = await self._scrape_team_page(domain)
            for member in team_members:
                title = member.get("title", "")
                if not any(
                    t.lower() in title.lower() for t in self.config.icp.titles
                ):
                    continue

                email = await find_email(
                    member.get("first_name", ""),
                    member.get("last_name", ""),
                    domain,
                ) or ""

                if await self.state.prospect_exists(email=email):
                    continue

                prospect = Prospect(
                    id="",
                    first_name=member.get("first_name", ""),
                    last_name=member.get("last_name", ""),
                    email=email,
                    company=company_name,
                    title=title,
                    industry=self.config.icp.industries[0],
                    source="company_website",
                )
                await self.state.add_prospect(prospect)
                count += 1

        return count

    async def _google_search(self, query: str) -> list[tuple[str, str]]:
        """Perform a Google search and return (url, snippet) pairs."""
        encoded = quote_plus(query)
        url = f"https://www.google.com/search?q={encoded}&num=20"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36"
                        )
                    },
                    follow_redirects=True,
                    timeout=15,
                )

            if resp.status_code != 200:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            results = []
            for div in soup.select("div.g"):
                link = div.select_one("a")
                snippet_el = div.select_one("div.VwiC3b")
                if link and link.get("href"):
                    href = link["href"]
                    snippet = snippet_el.get_text() if snippet_el else ""
                    results.append((href, snippet))

            return results[:20]

        except Exception as e:
            logger.debug(f"Google search failed: {e}")
            return []

    async def _scrape_team_page(self, domain: str) -> list[dict]:
        """Try to find and scrape a company's team/about page."""
        team_paths = ["/team", "/about", "/about-us", "/our-team", "/people"]
        members = []

        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            for path in team_paths:
                try:
                    url = f"https://{domain}{path}"
                    resp = await client.get(
                        url,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "html.parser")

                    # Look for common team member patterns
                    # Many sites use cards with name + title
                    for card in soup.select(
                        ".team-member, .person, .staff, [class*='team']"
                    ):
                        name_el = card.select_one(
                            "h2, h3, h4, .name, [class*='name']"
                        )
                        title_el = card.select_one(
                            "p, .title, .role, [class*='title'], [class*='role']"
                        )
                        if name_el:
                            name = name_el.get_text().strip()
                            title = title_el.get_text().strip() if title_el else ""
                            parts = name.split(" ", 1)
                            members.append({
                                "first_name": parts[0],
                                "last_name": parts[1] if len(parts) > 1 else "",
                                "title": title,
                            })

                    if members:
                        break

                except Exception:
                    continue

        return members

    async def _guess_domain(self, company_name: str) -> str:
        """Guess a company's domain from its name."""
        # Simple heuristic: lowercase, remove common suffixes, add .com
        name = company_name.lower().strip()
        for suffix in [" inc", " llc", " ltd", " corp", " co", " group"]:
            name = name.replace(suffix, "")
        name = re.sub(r"[^a-z0-9]", "", name)
        domain = f"{name}.com"

        # Verify the domain exists
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.head(
                    f"https://{domain}", follow_redirects=True
                )
                if resp.status_code < 400:
                    return domain
        except Exception:
            pass

        return ""

    async def _get_personalization(self, prospect: Prospect) -> str:
        """Ask brain for personalization angles based on prospect info."""
        prompt = f"""Based on this prospect, suggest 2-3 short personalization angles
for cold outreach. Keep each to one sentence.

Name: {prospect.full_name()}
Title: {prospect.title}
Company: {prospect.company}
Industry: {prospect.industry}

Our product: {self.config.product.description}

Return just the personalization notes, no labels or bullets."""

        return await self.brain.think(prompt, session_id="harvey-scout") or ""
