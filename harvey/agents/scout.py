"""Scout — DIY prospecting without expensive tools.

Architecture: Python does ALL web searching/scraping. Claude only
analyzes, scores, and personalizes data that Python already found.
This avoids model-level refusals when asking Claude to research
real people/companies.

Search backends (in priority order):
1. SERP API (Serper.dev) — if SERPER_API_KEY is set, reliable and fast
2. DuckDuckGo HTML — free, no rate limiting, good fallback
3. Bing — free, less aggressive than Google
4. Google — aggressive rate limiting, used last

Each cycle runs 2-3 queries max to avoid rate limits. Queries are
tracked in the DB so they spread across heartbeat cycles.
"""

import asyncio
import json
import logging
import random
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

# Max queries per cycle to avoid rate limits
MAX_QUERIES_PER_CYCLE = 3
# Delay between search requests (seconds)
SEARCH_DELAY = (2, 5)


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
        self._queries_this_cycle = 0

    async def run(self):
        """Main prospecting flow: find leads matching ICP."""
        logger.info("Scout: Starting prospecting cycle...")

        self.skills = self.brain.load_skills_for_agent("scout")
        self._queries_this_cycle = 0

        prospects_found = 0

        # Strategy 1: LinkedIn search (if enabled)
        if self.config.channels.linkedin.enabled and self.env.linkedin_email:
            prospects_found += await self._prospect_via_linkedin()

        # Strategy 2: Search for LinkedIn profiles via web search
        prospects_found += await self._prospect_via_profile_search()

        # Strategy 3: Discover companies, then scrape team pages
        prospects_found += await self._prospect_via_company_discovery()

        await self.state.log_action(
            action_type="prospect",
            agent="scout",
            details={"prospects_found": prospects_found},
        )
        logger.info(f"Scout: Found {prospects_found} new prospects this cycle.")

    # ── Search backend abstraction ──

    async def _web_search(self, query: str) -> list[tuple[str, str]]:
        """Search the web using the best available backend.

        Returns list of (url, snippet) tuples.
        Tries backends in order: Serper API → DuckDuckGo → Bing → Google.
        """
        if self._queries_this_cycle >= MAX_QUERIES_PER_CYCLE:
            logger.info("Scout: Query limit reached for this cycle. Will continue next cycle.")
            return []

        self._queries_this_cycle += 1

        # Add a random delay between searches to be polite
        if self._queries_this_cycle > 1:
            delay = random.uniform(*SEARCH_DELAY)
            await asyncio.sleep(delay)

        # Try each backend in order
        serper_key = getattr(self.env, "serper_api_key", "")
        if serper_key:
            results = await self._search_serper(query, serper_key)
            if results:
                return results

        results = await self._search_duckduckgo(query)
        if results:
            return results

        results = await self._search_bing(query)
        if results:
            return results

        results = await self._search_google(query)
        if results:
            return results

        logger.warning(f"Scout: All search backends failed for: {query[:80]}")
        return []

    async def _search_serper(self, query: str, api_key: str) -> list[tuple[str, str]]:
        """Search via Serper.dev API — most reliable, $5/mo for 2.5k searches."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    json={"q": query, "num": 20},
                    headers={
                        "X-API-KEY": api_key,
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code != 200:
                    logger.debug(f"Serper returned {resp.status_code}")
                    return []

                data = resp.json()
                results = []
                for item in data.get("organic", []):
                    url = item.get("link", "")
                    snippet = item.get("snippet", "")
                    if url:
                        results.append((url, snippet))
                return results[:20]

        except Exception as e:
            logger.debug(f"Serper search failed: {e}")
            return []

    async def _search_duckduckgo(self, query: str) -> list[tuple[str, str]]:
        """Search via DuckDuckGo HTML — free, no rate limiting."""
        encoded = quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                )

            if resp.status_code != 200:
                logger.debug(f"DuckDuckGo returned {resp.status_code}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            results = []

            # DuckDuckGo HTML results use .result class with .result__a links
            for result in soup.select(".result"):
                link = result.select_one(".result__a")
                snippet_el = result.select_one(".result__snippet")
                if link and link.get("href"):
                    href = link["href"]
                    # DDG wraps URLs in a redirect — extract the real URL
                    if "uddg=" in href:
                        from urllib.parse import parse_qs, urlparse as _urlparse
                        parsed = _urlparse(href)
                        params = parse_qs(parsed.query)
                        real_urls = params.get("uddg", [])
                        href = real_urls[0] if real_urls else href
                    snippet = snippet_el.get_text().strip() if snippet_el else ""
                    results.append((href, snippet))

            if results:
                logger.debug(f"DuckDuckGo returned {len(results)} results")
            return results[:20]

        except Exception as e:
            logger.debug(f"DuckDuckGo search failed: {e}")
            return []

    async def _search_bing(self, query: str) -> list[tuple[str, str]]:
        """Search via Bing HTML scraping — less aggressive rate limiting than Google."""
        encoded = quote_plus(query)
        url = f"https://www.bing.com/search?q={encoded}&count=20"

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                )

            if resp.status_code != 200:
                logger.debug(f"Bing returned {resp.status_code}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            results = []

            # Bing uses <li class="b_algo"> for organic results
            for item in soup.select("li.b_algo"):
                link = item.select_one("h2 a")
                snippet_el = item.select_one(".b_caption p")
                if link and link.get("href"):
                    href = link["href"]
                    snippet = snippet_el.get_text().strip() if snippet_el else ""
                    results.append((href, snippet))

            if results:
                logger.debug(f"Bing returned {len(results)} results")
            return results[:20]

        except Exception as e:
            logger.debug(f"Bing search failed: {e}")
            return []

    async def _search_google(self, query: str) -> list[tuple[str, str]]:
        """Search via Google HTML scraping — aggressive rate limiting, use as last resort."""
        encoded = quote_plus(query)
        url = f"https://www.google.com/search?q={encoded}&num=20"

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                )

            if resp.status_code == 429:
                logger.info("Scout: Google rate limited (429). Skipping Google.")
                return []

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

            if results:
                logger.debug(f"Google returned {len(results)} results")
            return results[:20]

        except Exception as e:
            logger.debug(f"Google search failed: {e}")
            return []

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

    # ── Strategy 2: Web search → LinkedIn profiles ──

    async def _prospect_via_profile_search(self) -> int:
        """Find LinkedIn profiles via web search (any backend)."""
        logger.info("Scout: Searching for LinkedIn profiles...")
        count = 0

        for title in self.config.icp.titles:
            if self._queries_this_cycle >= MAX_QUERIES_PER_CYCLE:
                break

            geo = self.config.icp.geography[0] if self.config.icp.geography else ""
            industry = self.config.icp.industries[0] if self.config.icp.industries else ""

            query = f'site:linkedin.com/in "{title}" "{industry}"'
            if geo:
                query += f' "{geo}"'

            results = await self._web_search(query)

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
                    source="web_search",
                    source_url=url,
                )

                if not prospect.is_valid():
                    continue

                await self.state.add_prospect(prospect)
                count += 1

                if count >= 15:
                    break

            if count >= 15:
                break

        return count

    # ── Strategy 3: Find companies, then scrape team pages ──

    async def _prospect_via_company_discovery(self) -> int:
        """Find companies via multiple methods, scrape team pages for contacts.

        Python does ALL the searching and scraping. Claude only scores/personalizes
        the contacts that Python already found.
        """
        logger.info("Scout: Discovering companies...")

        # Gather companies from multiple sources
        companies = []

        # Source 1: Web search for companies
        companies.extend(await self._find_companies_via_search())

        # Source 2: Industry directories
        companies.extend(await self._find_companies_via_directories())

        if not companies:
            logger.info("Scout: No new companies found this cycle.")
            return 0

        # Deduplicate by domain
        seen = set()
        unique = []
        for c in companies:
            if c["domain"] not in seen:
                seen.add(c["domain"])
                unique.append(c)
        companies = unique[:10]

        logger.info(f"Scout: Found {len(companies)} candidate companies.")

        # For each company, scrape team page and find contacts
        all_contacts = []
        for company in companies:
            company_id = await self._ensure_company(
                name=company["name"],
                domain=company["domain"],
                website=company.get("website", f"https://{company['domain']}"),
                description=company.get("description", ""),
                industry=company.get("industry", self.config.icp.industries[0] if self.config.icp.industries else ""),
                source=company.get("source", "web_search"),
                source_url=company.get("source_url", ""),
            )

            team_members = await self._scrape_team_page(company["domain"])
            for member in team_members:
                title = member.get("title", "")
                if not self._title_matches_icp(title):
                    continue

                first_name = member.get("first_name", "")
                last_name = member.get("last_name", "")
                if not first_name or not last_name:
                    continue

                email = ""
                email_verified = False
                found = await find_email(first_name, last_name, company["domain"])
                if found:
                    email = found
                    email_verified = True

                if await self.state.prospect_exists(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    company=company["name"],
                ):
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

        # Use Claude ONLY to score and personalize
        scored_contacts = await self._score_contacts(all_contacts)

        count = 0
        for prospect in scored_contacts:
            await self.state.add_prospect(prospect)
            count += 1
            logger.info(
                f"Scout: Added {prospect.full_name()} ({prospect.title}) "
                f"at {prospect.company} [score: {prospect.score}]"
            )

        return count

    async def _find_companies_via_search(self) -> list[dict]:
        """Find ICP-matching companies via web search."""
        companies = []
        seen_domains = set()

        queries = self._build_company_search_queries()

        for query in queries:
            if self._queries_this_cycle >= MAX_QUERIES_PER_CYCLE:
                break

            results = await self._web_search(query)

            for url, snippet in results:
                domain = self._extract_domain(url)
                if not domain or domain in seen_domains:
                    continue
                if self._is_noise_domain(domain):
                    continue
                if await self.state.company_exists(domain):
                    seen_domains.add(domain)
                    continue

                seen_domains.add(domain)

                company_info = await self._scrape_company_info(domain)
                company_info["domain"] = domain
                company_info["source"] = "web_search"
                company_info["source_url"] = url
                if not company_info.get("name"):
                    company_info["name"] = self._domain_to_name(domain)

                companies.append(company_info)

                if len(companies) >= 5:
                    break

            if len(companies) >= 5:
                break

        return companies

    async def _find_companies_via_directories(self) -> list[dict]:
        """Scrape industry directory sites for company names and domains.

        These sites list companies by category and are much more reliable
        than Google for finding ICP-matching companies.
        """
        companies = []
        seen_domains = set()

        industries = self.config.icp.industries or []
        if not industries:
            return []

        # Build directory-specific queries
        directory_queries = []
        for industry in industries[:2]:
            # G2, Capterra, Clutch etc. have category pages
            directory_queries.extend([
                f'site:g2.com/categories "{industry}"',
                f'site:clutch.co "{industry}" companies',
                f'"{industry}" company directory list',
            ])

        for query in directory_queries:
            if self._queries_this_cycle >= MAX_QUERIES_PER_CYCLE:
                break

            results = await self._web_search(query)

            for url, snippet in results:
                # From directory results, try to extract mentioned company domains
                domains = self._extract_company_domains_from_snippet(snippet, url)
                for domain in domains:
                    if domain in seen_domains or self._is_noise_domain(domain):
                        continue
                    if await self.state.company_exists(domain):
                        seen_domains.add(domain)
                        continue

                    seen_domains.add(domain)
                    company_info = await self._scrape_company_info(domain)
                    company_info["domain"] = domain
                    company_info["source"] = "directory"
                    company_info["source_url"] = url
                    if not company_info.get("name"):
                        company_info["name"] = self._domain_to_name(domain)

                    companies.append(company_info)

                    if len(companies) >= 5:
                        break

                # Also scrape the directory page itself for company links
                if len(companies) < 5:
                    page_companies = await self._scrape_directory_page(url)
                    for pc in page_companies:
                        if pc["domain"] in seen_domains or self._is_noise_domain(pc["domain"]):
                            continue
                        if await self.state.company_exists(pc["domain"]):
                            seen_domains.add(pc["domain"])
                            continue
                        seen_domains.add(pc["domain"])
                        companies.append(pc)
                        if len(companies) >= 5:
                            break

            if len(companies) >= 5:
                break

        return companies

    async def _scrape_directory_page(self, url: str) -> list[dict]:
        """Scrape a directory/list page for company links."""
        companies = []

        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36"
                        ),
                    },
                )
                if resp.status_code != 200:
                    return []

                soup = BeautifulSoup(resp.text, "html.parser")

                # Look for external links that could be company websites
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    if not href.startswith("http"):
                        continue

                    domain = self._extract_domain(href)
                    if not domain or self._is_noise_domain(domain):
                        continue

                    # Skip links back to the directory itself
                    page_domain = self._extract_domain(url)
                    if domain == page_domain:
                        continue

                    link_text = link.get_text().strip()
                    if len(link_text) > 3 and len(link_text) < 60:
                        companies.append({
                            "name": link_text,
                            "domain": domain,
                            "website": f"https://{domain}",
                            "source": "directory",
                            "source_url": url,
                        })

                    if len(companies) >= 10:
                        break

        except Exception as e:
            logger.debug(f"Failed to scrape directory page {url}: {e}")

        return companies

    def _extract_company_domains_from_snippet(self, snippet: str, source_url: str) -> list[str]:
        """Extract potential company domains mentioned in a search snippet."""
        # Look for domain-like patterns in the snippet
        domains = []
        domain_pattern = re.findall(r'\b([a-zA-Z0-9-]+\.(?:com|io|co|net|org|ai))\b', snippet)
        for d in domain_pattern:
            d = d.lower()
            if not self._is_noise_domain(d) and len(d) > 5:
                domains.append(d)
        return domains[:5]

    def _build_company_search_queries(self) -> list[str]:
        """Build search queries to find ICP-matching companies."""
        queries = []
        industries = self.config.icp.industries or [""]
        geos = self.config.icp.geography or [""]
        size = self.config.icp.company_size or ""

        for industry in industries[:2]:
            geo = geos[0] if geos else ""

            # Direct search
            parts = [f'"{industry}"']
            if geo:
                parts.append(f'"{geo}"')
            if size:
                parts.append(f'"{size}"')
            parts.append("company")
            queries.append(" ".join(parts))

            # Industry list search
            if geo:
                queries.append(f'top "{industry}" companies {geo}')

        return queries[:3]  # Cap to stay within query budget

    async def _score_contacts(self, contacts: list[Prospect]) -> list[Prospect]:
        """Use Claude to score and add personalization notes to found contacts.

        This is the ONLY place Claude is used in the scout. It receives
        already-found data and just scores/personalizes it.
        """
        if not contacts:
            return contacts

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
            logger.warning("Scout: Claude scoring failed, using default scores.")
            for c in contacts:
                c.score = 50
            return contacts

        for item in result:
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(contacts):
                contacts[idx].score = item.get("score", 50)
                contacts[idx].personalization_notes = item.get("personalization", "")

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

    async def _scrape_company_info(self, domain: str) -> dict:
        """Scrape a company's homepage for basic info. Pure Python."""
        info = {"name": "", "description": "", "website": f"https://{domain}"}

        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(
                    f"https://{domain}",
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36"
                        ),
                    },
                )
                if resp.status_code != 200:
                    return info

                soup = BeautifulSoup(resp.text, "html.parser")

                title_tag = soup.find("title")
                if title_tag:
                    title_text = title_tag.get_text().strip()
                    info["name"] = title_text.split("|")[0].split("—")[0].split("-")[0].strip()

                meta_desc = soup.find("meta", attrs={"name": "description"})
                if meta_desc and meta_desc.get("content"):
                    info["description"] = meta_desc["content"].strip()[:500]

                og_name = soup.find("meta", attrs={"property": "og:site_name"})
                if og_name and og_name.get("content"):
                    info["name"] = og_name["content"].strip()

        except Exception as e:
            logger.debug(f"Failed to scrape {domain}: {e}")

        return info

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

    def _extract_domain(self, url: str) -> str:
        """Extract the root domain from a URL."""
        try:
            parsed = urlparse(url)
            host = parsed.netloc or parsed.path.split("/")[0]
            host = re.sub(r"^www\.", "", host)
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
            "google.com", "bing.com", "yahoo.com", "duckduckgo.com",
            "wikipedia.org", "reddit.com", "quora.com",
            "yelp.com", "bbb.org", "glassdoor.com",
            "crunchbase.com", "zoominfo.com", "apollo.io",
            "indeed.com", "monster.com",
            "github.com", "stackoverflow.com",
            "medium.com", "substack.com",
            "amazon.com", "apple.com", "microsoft.com",
            "g2.com", "capterra.com", "clutch.co",
            "trustpilot.com", "getapp.com",
        }
        return domain in noise or any(domain.endswith(f".{n}") for n in noise)

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

    def _parse_linkedin_url(self, url: str, snippet: str) -> dict | None:
        """Parse a LinkedIn profile URL and snippet to extract name."""
        match = re.search(r"/in/([\w][\w-]+)", url)
        if match:
            slug = match.group(1)
            # Remove trailing hex/numeric IDs that LinkedIn appends
            slug = re.sub(r"-[0-9a-f]{4,}$", "", slug)
            parts = slug.split("-")
            # Filter out empty parts and very short noise
            parts = [p for p in parts if len(p) > 1 or p.isalpha()]
            if len(parts) >= 2:
                return {
                    "first_name": parts[0].title(),
                    "last_name": " ".join(p.title() for p in parts[1:]),
                }

        # Fallback: parse from snippet (e.g., "First Last - Title at Company")
        name_match = re.match(r"^([A-Z][a-z]+)\s+([A-Z][a-z]+)", snippet)
        if name_match:
            return {
                "first_name": name_match.group(1),
                "last_name": name_match.group(2),
            }

        return None
