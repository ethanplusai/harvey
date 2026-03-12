"""Scout — DIY prospecting without expensive tools.

Architecture: Python does ALL web searching/scraping. Claude only
analyzes, scores, and personalizes data that Python already found.
This avoids model-level refusals when asking Claude to research
real people/companies.
"""

import json
import logging
import re
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from harvey.brain import Brain
from harvey.config import HarveyConfig, EnvConfig
from harvey.integrations.email_finder import find_email
from harvey.integrations.linkedin import LinkedInAutomation
from harvey.models.company import Company
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
        self.skills = ""

    async def run(self):
        """Main prospecting flow: find leads matching ICP."""
        logger.info("Scout: Starting prospecting cycle...")

        self.skills = self.brain.load_skills_for_agent("scout")

        prospects_found = 0

        # Strategy 1: LinkedIn search (if enabled)
        if self.config.channels.linkedin.enabled and self.env.linkedin_email:
            prospects_found += await self._prospect_via_linkedin()

        # Strategy 2: Google dorking for LinkedIn profiles
        prospects_found += await self._prospect_via_google_profiles()

        # Strategy 3: Google dorking for companies, then scrape team pages
        prospects_found += await self._prospect_via_company_discovery()

        await self.state.log_action(
            action_type="prospect",
            agent="scout",
            details={"prospects_found": prospects_found},
        )
        logger.info(f"Scout: Found {prospects_found} new prospects this cycle.")

    # ── Strategy 1: LinkedIn ──

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
                        if await self.state.prospect_exists(
                            linkedin_url=profile.get("linkedin_url", "")
                        ):
                            continue

                        company_name = profile.get("company", "")
                        domain = ""
                        company_id = ""
                        if company_name:
                            domain = await self._guess_domain(company_name)
                            if domain:
                                company_id = await self._ensure_company(
                                    name=company_name,
                                    domain=domain,
                                    industry=industry,
                                    source="linkedin_search",
                                )

                        email = ""
                        email_verified = False
                        if domain and profile.get("first_name") and profile.get("last_name"):
                            found = await find_email(
                                profile["first_name"],
                                profile["last_name"],
                                domain,
                            )
                            if found:
                                email = found
                                email_verified = True

                        prospect = Prospect(
                            first_name=profile.get("first_name", ""),
                            last_name=profile.get("last_name", ""),
                            email=email,
                            email_verified=email_verified,
                            linkedin_url=profile.get("linkedin_url", ""),
                            company=company_name,
                            company_id=company_id,
                            title=profile.get("title", ""),
                            seniority=self._infer_seniority(profile.get("title", "")),
                            industry=industry,
                            source="linkedin_search",
                            source_url=profile.get("linkedin_url", ""),
                        )

                        if not prospect.is_valid():
                            continue

                        await self.state.add_prospect(prospect)
                        count += 1
                        logger.info(
                            f"Scout: Added prospect {prospect.full_name()} "
                            f"at {company_name}"
                        )

            return count

        finally:
            await linkedin.stop()

    # ── Strategy 2: Google → LinkedIn profiles ──

    async def _prospect_via_google_profiles(self) -> int:
        """Use Google dorking to find LinkedIn profiles matching ICP."""
        logger.info("Scout: Google dorking for LinkedIn profiles...")
        count = 0

        for title in self.config.icp.titles:
            for geo in self.config.icp.geography:
                query = (
                    f'site:linkedin.com/in "{title}" '
                    f'"{self.config.icp.industries[0]}" "{geo}"'
                )
                results = await self._google_search(query)

                for url, snippet in results:
                    if "/in/" not in url:
                        continue
                    if await self.state.prospect_exists(linkedin_url=url):
                        continue

                    parsed = self._parse_linkedin_url(url, snippet)
                    if not parsed:
                        continue

                    prospect = Prospect(
                        first_name=parsed.get("first_name", ""),
                        last_name=parsed.get("last_name", ""),
                        linkedin_url=url,
                        title=title,
                        seniority=self._infer_seniority(title),
                        source="google_dork",
                        source_url=url,
                    )

                    if not prospect.is_valid():
                        continue

                    await self.state.add_prospect(prospect)
                    count += 1

                if count >= 20:
                    break
            if count >= 20:
                break

        return count

    # ── Strategy 3: Find companies via Google, then scrape team pages ──

    async def _prospect_via_company_discovery(self) -> int:
        """Find companies via Google search, scrape their team pages for contacts.

        Python does ALL the searching and scraping. Claude only scores/personalizes
        the contacts that Python already found.
        """
        logger.info("Scout: Discovering companies via Google...")

        # Step 1: Python finds companies via Google dorking
        companies = await self._find_companies_via_google()
        if not companies:
            logger.info("Scout: No new companies found via Google.")
            return 0

        logger.info(f"Scout: Found {len(companies)} candidate companies.")

        # Step 2: For each company, scrape team page and find contacts
        all_contacts = []
        for company in companies:
            # Save the company
            company_id = await self._ensure_company(
                name=company["name"],
                domain=company["domain"],
                website=company.get("website", f"https://{company['domain']}"),
                description=company.get("description", ""),
                industry=company.get("industry", self.config.icp.industries[0] if self.config.icp.industries else ""),
                source="google_dork",
                source_url=company.get("source_url", ""),
            )

            # Scrape their team page for contacts
            team_members = await self._scrape_team_page(company["domain"])
            for member in team_members:
                title = member.get("title", "")
                # Filter to ICP-matching titles
                if not self._title_matches_icp(title):
                    continue

                first_name = member.get("first_name", "")
                last_name = member.get("last_name", "")
                if not first_name or not last_name:
                    continue

                # Try to find and verify their email
                email = ""
                email_verified = False
                found = await find_email(first_name, last_name, company["domain"])
                if found:
                    email = found
                    email_verified = True

                if email and await self.state.prospect_exists(email=email):
                    continue

                prospect = Prospect(
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    email_verified=email_verified,
                    company=company["name"],
                    company_id=company_id,
                    title=title,
                    seniority=self._infer_seniority(title),
                    industry=company.get("industry", ""),
                    source="company_website",
                    source_url=f"https://{company['domain']}",
                )

                if prospect.is_valid():
                    all_contacts.append(prospect)

        if not all_contacts:
            logger.info("Scout: No ICP-matching contacts found on team pages.")
            return 0

        # Step 3: Use Claude ONLY to score and personalize the contacts we found
        scored_contacts = await self._score_contacts(all_contacts)

        # Step 4: Save contacts
        count = 0
        for prospect in scored_contacts:
            await self.state.add_prospect(prospect)
            count += 1
            logger.info(
                f"Scout: Added {prospect.full_name()} ({prospect.title}) "
                f"at {prospect.company} [score: {prospect.score}]"
            )

        return count

    async def _find_companies_via_google(self) -> list[dict]:
        """Find ICP-matching companies via Google dorking. Pure Python, no Claude."""
        companies = []
        seen_domains = set()

        search_queries = self._build_company_search_queries()

        for query in search_queries:
            results = await self._google_search(query)

            for url, snippet in results:
                domain = self._extract_domain(url)
                if not domain or domain in seen_domains:
                    continue

                # Skip social media, directories, and other non-company sites
                if self._is_noise_domain(domain):
                    continue

                # Check if we already have this company
                if await self.state.company_exists(domain):
                    seen_domains.add(domain)
                    continue

                seen_domains.add(domain)

                # Try to get company info from their homepage
                company_info = await self._scrape_company_info(domain)
                company_info["domain"] = domain
                company_info["source_url"] = url
                if not company_info.get("name"):
                    company_info["name"] = self._domain_to_name(domain)

                companies.append(company_info)

                if len(companies) >= 10:
                    break

            if len(companies) >= 10:
                break

        return companies

    def _build_company_search_queries(self) -> list[str]:
        """Build Google search queries to find ICP-matching companies."""
        queries = []
        industries = self.config.icp.industries or [""]
        geos = self.config.icp.geography or [""]
        size = self.config.icp.company_size or ""

        for industry in industries[:3]:
            for geo in geos[:2]:
                # Direct industry + geography search
                parts = [f'"{industry}"']
                if geo:
                    parts.append(f'"{geo}"')
                if size:
                    parts.append(f'"{size}"')
                parts.append("company")
                queries.append(" ".join(parts))

                # Search for industry directories/lists
                queries.append(f'"{industry}" companies {geo} list')

        return queries[:6]  # Cap at 6 queries per cycle

    def _extract_domain(self, url: str) -> str:
        """Extract the root domain from a URL."""
        try:
            parsed = urlparse(url)
            host = parsed.netloc or parsed.path.split("/")[0]
            # Remove www prefix
            host = re.sub(r"^www\.", "", host)
            # Basic validation
            if "." in host and len(host) > 3:
                return host.lower()
        except Exception:
            pass
        return ""

    def _is_noise_domain(self, domain: str) -> bool:
        """Filter out domains that aren't actual companies."""
        noise = {
            "linkedin.com", "facebook.com", "twitter.com", "x.com",
            "instagram.com", "youtube.com", "tiktok.com",
            "google.com", "bing.com", "yahoo.com",
            "wikipedia.org", "reddit.com", "quora.com",
            "yelp.com", "bbb.org", "glassdoor.com",
            "crunchbase.com", "zoominfo.com", "apollo.io",
            "indeed.com", "monster.com",
            "github.com", "stackoverflow.com",
            "medium.com", "substack.com",
            "amazon.com", "apple.com", "microsoft.com",
        }
        return domain in noise or any(domain.endswith(f".{n}") for n in noise)

    async def _scrape_company_info(self, domain: str) -> dict:
        """Scrape a company's homepage for basic info. Pure Python."""
        info = {"name": "", "description": "", "website": f"https://{domain}"}

        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(
                    f"https://{domain}",
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                )
                if resp.status_code != 200:
                    return info

                soup = BeautifulSoup(resp.text, "html.parser")

                # Get company name from <title> or <meta>
                title_tag = soup.find("title")
                if title_tag:
                    title_text = title_tag.get_text().strip()
                    # Clean up common title patterns: "Company - Tagline"
                    info["name"] = title_text.split("|")[0].split("—")[0].split("-")[0].strip()

                # Get description from meta
                meta_desc = soup.find("meta", attrs={"name": "description"})
                if meta_desc and meta_desc.get("content"):
                    info["description"] = meta_desc["content"].strip()[:500]

                # Try og:site_name for a cleaner company name
                og_name = soup.find("meta", attrs={"property": "og:site_name"})
                if og_name and og_name.get("content"):
                    info["name"] = og_name["content"].strip()

        except Exception as e:
            logger.debug(f"Failed to scrape {domain}: {e}")

        return info

    def _domain_to_name(self, domain: str) -> str:
        """Convert a domain to a rough company name."""
        name = domain.split(".")[0]
        return name.title()

    def _title_matches_icp(self, title: str) -> bool:
        """Check if a job title matches the ICP target titles."""
        if not title:
            return False
        title_lower = title.lower()
        for target in self.config.icp.titles:
            if target.lower() in title_lower:
                return True
        # Also accept common senior titles if no specific titles configured
        if not self.config.icp.titles:
            senior_keywords = ["ceo", "cto", "cfo", "vp", "director", "head of", "founder"]
            return any(kw in title_lower for kw in senior_keywords)
        return False

    def _infer_seniority(self, title: str) -> str:
        """Infer seniority level from a job title."""
        if not title:
            return ""
        t = title.lower()
        if any(kw in t for kw in ["ceo", "cto", "cfo", "cmo", "coo", "cro", "chief", "founder", "co-founder"]):
            return "c_suite"
        if any(kw in t for kw in ["vp", "vice president", "svp", "evp", "head of"]):
            return "vp"
        if "director" in t:
            return "director"
        if any(kw in t for kw in ["manager", "team lead"]):
            return "manager"
        return "individual"

    async def _score_contacts(self, contacts: list[Prospect]) -> list[Prospect]:
        """Use Claude to score and add personalization notes to found contacts.

        This is the ONLY place Claude is used in the scout. It receives
        already-found data and just scores/personalizes it. It never
        searches for or researches people.
        """
        if not contacts:
            return contacts

        # Build a summary of all contacts for Claude to score in one call
        contact_summaries = []
        for i, c in enumerate(contacts):
            contact_summaries.append(
                f"{i+1}. {c.full_name()} — {c.title} at {c.company} "
                f"({c.industry}). Email: {c.email or 'none'}. "
                f"Seniority: {c.seniority or 'unknown'}."
            )

        prompt = f"""You are analyzing a batch of sales prospects that have already been found and verified.
Your job is to score each one (1-100) based on how well they match the ICP, and add a short personalization note.

Our product: {self.config.product.description}
Target industries: {', '.join(self.config.icp.industries)}
Target titles: {', '.join(self.config.icp.titles)}
Target company size: {self.config.icp.company_size}

Here are the prospects to score:
{chr(10).join(contact_summaries)}

Return a JSON array with one object per prospect, in the same order:
[{{"index": 1, "score": 75, "personalization": "Short angle for outreach"}}, ...]

Score criteria:
- 80-100: Perfect ICP match, right title, right industry, right company size
- 60-79: Good match, close to ICP
- 40-59: Partial match, might be worth a shot
- 1-39: Poor match, probably skip

Respond ONLY with the JSON array."""

        result = await self.brain.think_json(prompt, session_id="harvey-scout-score")

        if not result or not isinstance(result, list):
            # If Claude fails, just return contacts with default scores
            logger.warning("Scout: Claude scoring failed, using default scores.")
            for c in contacts:
                c.score = 50
            return contacts

        # Apply scores and personalization
        for item in result:
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(contacts):
                contacts[idx].score = item.get("score", 50)
                contacts[idx].personalization_notes = item.get("personalization", "")

        # Sort by score descending, filter out very low scores
        contacts = [c for c in contacts if c.score >= 30]
        contacts.sort(key=lambda c: c.score, reverse=True)

        return contacts

    # ── Shared utilities ──

    async def _ensure_company(
        self,
        name: str,
        domain: str,
        website: str = "",
        description: str = "",
        industry: str = "",
        company_size: str = "",
        location: str = "",
        source: str = "",
        source_url: str = "",
    ) -> str:
        """Get or create a company record. Returns company_id."""
        existing = await self.state.get_company_by_domain(domain)
        if existing:
            return existing.id

        company = Company(
            name=name,
            domain=domain,
            website=website or f"https://{domain}",
            description=description,
            industry=industry,
            company_size=company_size,
            location=location,
            source=source,
            source_url=source_url,
        )
        return await self.state.add_company(company)

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
        team_paths = ["/team", "/about", "/about-us", "/our-team", "/people",
                      "/leadership", "/about/team", "/company/team"]
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
                    for card in soup.select(
                        ".team-member, .person, .staff, [class*='team'], "
                        "[class*='leadership'], [class*='member'], [class*='person']"
                    ):
                        name_el = card.select_one(
                            "h2, h3, h4, .name, [class*='name']"
                        )
                        title_el = card.select_one(
                            "p, .title, .role, .position, "
                            "[class*='title'], [class*='role'], [class*='position']"
                        )
                        if name_el:
                            name = name_el.get_text().strip()
                            title = title_el.get_text().strip() if title_el else ""
                            # Skip if name looks like a section header
                            if len(name) > 50 or len(name) < 3:
                                continue
                            parts = name.split(" ", 1)
                            if len(parts) >= 2:
                                members.append({
                                    "first_name": parts[0].strip(),
                                    "last_name": parts[1].strip(),
                                    "title": title,
                                })

                    if members:
                        break

                except Exception:
                    continue

        return members

    async def _guess_domain(self, company_name: str) -> str:
        """Guess a company's domain from its name."""
        name = company_name.lower().strip()
        for suffix in [" inc", " llc", " ltd", " corp", " co", " group"]:
            name = name.replace(suffix, "")
        name = re.sub(r"[^a-z0-9]", "", name)
        domain = f"{name}.com"

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

    def _parse_linkedin_url(self, url: str, snippet: str) -> dict | None:
        """Parse a LinkedIn profile URL and snippet to extract name."""
        # LinkedIn URLs: linkedin.com/in/first-last-abc123
        match = re.search(r"/in/([a-zA-Z]+-[a-zA-Z]+)", url)
        if match:
            parts = match.group(1).split("-")
            if len(parts) >= 2:
                return {
                    "first_name": parts[0].title(),
                    "last_name": parts[1].title(),
                }

        # Try to parse from snippet: "First Last - Title at Company"
        name_match = re.match(r"^([A-Z][a-z]+)\s+([A-Z][a-z]+)", snippet)
        if name_match:
            return {
                "first_name": name_match.group(1),
                "last_name": name_match.group(2),
            }

        return None
