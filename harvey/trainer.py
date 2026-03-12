"""Trainer — give Harvey a URL and it learns everything about the product."""

import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

from harvey.brain import Brain
from harvey.state import StateManager

logger = logging.getLogger("harvey.trainer")

# Pages most likely to contain useful product/company info
PRIORITY_PATHS = [
    "/",
    "/about",
    "/about-us",
    "/pricing",
    "/features",
    "/product",
    "/products",
    "/solutions",
    "/services",
    "/how-it-works",
    "/why-us",
    "/case-studies",
    "/customers",
    "/testimonials",
    "/faq",
    "/contact",
    "/team",
    "/our-team",
]

MAX_PAGES = 15
MAX_CONTENT_PER_PAGE = 5000  # chars


class Trainer:
    def __init__(self):
        self.state = StateManager()
        self.brain = Brain(self.state)
        self.scraped_pages: dict[str, str] = {}

    async def train(self, url: str, output_path: str = "harvey.yaml"):
        """Train Harvey on a product website.

        1. Crawl the site (key pages)
        2. Extract all text content
        3. Ask Claude to analyze and extract product info
        4. Generate a complete harvey.yaml config
        """
        await self.state.init_db()

        domain = urlparse(url).netloc
        base_url = f"{urlparse(url).scheme}://{domain}"

        logger.info(f"Training Harvey on {base_url}...")
        print(f"\n{'='*60}")
        print(f"  Harvey Training Mode")
        print(f"  Learning from: {base_url}")
        print(f"{'='*60}\n")

        # Step 1: Crawl the site
        print("[1/5] Crawling website...")
        await self._crawl_site(base_url)

        if not self.scraped_pages:
            print("ERROR: Could not scrape any pages. Check the URL.")
            return

        print(f"      Scraped {len(self.scraped_pages)} pages.\n")

        # Step 2: Extract product information
        print("[2/5] Analyzing product information...")
        product_info = await self._extract_product_info()
        if not product_info:
            print("ERROR: Could not extract product information.")
            return
        print(f"      Found: {product_info.get('product_name', 'Unknown Product')}\n")

        # Step 3: Identify ICP
        print("[3/5] Identifying ideal customer profile...")
        icp_info = await self._extract_icp()
        print(f"      Target: {', '.join(icp_info.get('industries', ['Unknown']))}\n")

        # Step 4: Generate objection responses
        print("[4/5] Generating objection handling playbook...")
        objections = await self._generate_objections(product_info)
        print(f"      Prepared {len(objections)} objection responses.\n")

        # Step 5: Generate config
        print("[5/5] Building configuration...")
        config = self._build_config(product_info, icp_info, objections, domain)

        # Write the config
        config_path = Path(output_path)
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        print(f"\n{'='*60}")
        print(f"  Training complete!")
        print(f"  Config written to: {config_path}")
        print(f"{'='*60}")
        print(f"\n  Product: {config['product']['name']}")
        print(f"  Company: {config['persona']['company']}")
        print(f"  ICP: {', '.join(config['icp']['titles'])}")
        print(f"  Industries: {', '.join(config['icp']['industries'])}")
        print(f"\n  Review {config_path} and adjust as needed.")
        print(f"  Then run: python -m harvey\n")

        # Also generate custom prompts based on the product
        await self._generate_custom_prompts(product_info, icp_info)

        return config

    async def _crawl_site(self, base_url: str):
        """Crawl key pages of the website."""
        crawled = set()

        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            # First, try priority paths
            for path in PRIORITY_PATHS:
                if len(self.scraped_pages) >= MAX_PAGES:
                    break

                url = urljoin(base_url, path)
                if url in crawled:
                    continue
                crawled.add(url)

                content = await self._fetch_page(client, url)
                if content:
                    self.scraped_pages[path] = content
                    logger.debug(f"Scraped {url} ({len(content)} chars)")

            # Then discover linked pages from homepage
            if "/" in self.scraped_pages:
                discovered = self._find_internal_links(
                    self.scraped_pages["/"], base_url
                )
                for link_url in discovered:
                    if len(self.scraped_pages) >= MAX_PAGES:
                        break
                    if link_url in crawled:
                        continue
                    crawled.add(link_url)

                    path = urlparse(link_url).path
                    content = await self._fetch_page(client, link_url)
                    if content:
                        self.scraped_pages[path] = content

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> str:
        """Fetch a page and extract text content."""
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return ""

            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type:
                return ""

            soup = BeautifulSoup(resp.text, "html.parser")

            # Remove noise
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()

            text = soup.get_text(separator="\n", strip=True)

            # Clean up whitespace
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            text = "\n".join(lines)

            # Truncate if too long
            if len(text) > MAX_CONTENT_PER_PAGE:
                text = text[:MAX_CONTENT_PER_PAGE]

            return text

        except Exception as e:
            logger.debug(f"Failed to fetch {url}: {e}")
            return ""

    def _find_internal_links(self, html_content: str, base_url: str) -> list[str]:
        """Find internal links from page content (re-parse from raw isn't available,
        so we use the base_url to filter discovered links)."""
        # Since we only have text content at this point, we'd need the raw HTML.
        # For simplicity, we rely on PRIORITY_PATHS for discovery.
        return []

    async def _extract_product_info(self) -> dict:
        """Ask Claude to extract product information from scraped content."""
        # Combine all page content
        all_content = ""
        for path, content in self.scraped_pages.items():
            all_content += f"\n\n--- PAGE: {path} ---\n{content}"

        # Truncate to fit in a reasonable prompt
        if len(all_content) > 30000:
            all_content = all_content[:30000]

        prompt = f"""Analyze this website content and extract product/company information.

WEBSITE CONTENT:
{all_content}

Extract the following and return as JSON:
{{
  "company_name": "the company name",
  "product_name": "the main product or service name",
  "product_description": "2-3 sentence description of what the product does and who it's for",
  "pricing": "pricing info if available, otherwise 'Contact for pricing'",
  "key_benefits": ["benefit 1", "benefit 2", "benefit 3", "benefit 4", "benefit 5"],
  "features": ["feature 1", "feature 2", "feature 3"],
  "target_audience": "who this product is for",
  "value_proposition": "the core value proposition in one sentence",
  "competitors": ["competitor 1", "competitor 2"],
  "social_proof": ["any case studies, testimonials, or notable customers mentioned"],
  "tone": "describe the brand's communication tone in 3-4 words"
}}"""

        result = await self.brain.think_json(prompt, session_id="harvey-trainer")
        return result if isinstance(result, dict) else {}

    async def _extract_icp(self) -> dict:
        """Ask Claude to determine the ideal customer profile."""
        all_content = "\n".join(
            f"[{path}]: {content[:2000]}"
            for path, content in list(self.scraped_pages.items())[:8]
        )

        prompt = f"""Based on this website content, determine the Ideal Customer Profile (ICP).

WEBSITE CONTENT:
{all_content}

Determine who would buy this product and return as JSON:
{{
  "industries": ["industry 1", "industry 2", "industry 3"],
  "company_size": "employee range like '10-200 employees' or '50-500 employees'",
  "titles": ["decision maker title 1", "title 2", "title 3", "title 4"],
  "geography": ["country or region 1"],
  "buyer_persona": "A brief description of the ideal buyer — their role, challenges, and goals",
  "pain_points": ["pain point 1", "pain point 2", "pain point 3"],
  "buying_triggers": ["trigger event 1", "trigger event 2", "trigger event 3"]
}}"""

        result = await self.brain.think_json(prompt, session_id="harvey-trainer")
        return result if isinstance(result, dict) else {
            "industries": ["Technology"],
            "company_size": "10-200 employees",
            "titles": ["VP", "Director", "Head of"],
            "geography": ["United States"],
        }

    async def _generate_objections(self, product_info: dict) -> dict:
        """Generate objection handling responses based on the product."""
        prompt = f"""You are a sales expert. Based on this product, generate common objections
and ideal responses.

Product: {product_info.get('product_name', '')}
Description: {product_info.get('product_description', '')}
Pricing: {product_info.get('pricing', '')}
Competitors: {', '.join(product_info.get('competitors', []))}

Generate 6-8 common objections a prospect would raise, with concise responses (1-2 sentences).
Return as a JSON object where keys are the objection and values are the response.

Example:
{{
  "too expensive": "Most clients see ROI within 30 days. We can walk through the numbers together.",
  "we already use X": "Makes sense. The teams that switched from X tell us the biggest difference is..."
}}"""

        result = await self.brain.think_json(prompt, session_id="harvey-trainer")
        return result if isinstance(result, dict) else {}

    async def _generate_custom_prompts(self, product_info: dict, icp_info: dict):
        """Generate product-specific prompt enhancements."""
        product_name = product_info.get("product_name", "the product")
        social_proof = product_info.get("social_proof", [])
        pain_points = icp_info.get("pain_points", [])
        buying_triggers = icp_info.get("buying_triggers", [])

        # Create a product knowledge file in skills/
        knowledge = f"""# Product Knowledge: {product_name}

## What We Sell
{product_info.get('product_description', '')}

## Core Value Proposition
{product_info.get('value_proposition', '')}

## Key Benefits
{chr(10).join('- ' + b for b in product_info.get('key_benefits', []))}

## Features
{chr(10).join('- ' + f for f in product_info.get('features', []))}

## Pricing
{product_info.get('pricing', 'Contact for pricing')}

## Target Audience
{product_info.get('target_audience', '')}

## Competitors
{chr(10).join('- ' + c for c in product_info.get('competitors', []))}

## Social Proof & Case Studies
{chr(10).join('- ' + s for s in social_proof)}

## Prospect Pain Points
{chr(10).join('- ' + p for p in pain_points)}

## Buying Triggers
When these events happen, prospects are most likely to buy:
{chr(10).join('- ' + t for t in buying_triggers)}

## Tone & Voice
{product_info.get('tone', 'professional, consultative, confident')}
"""

        skills_path = Path(__file__).parent.parent / "skills" / "product_knowledge.md"
        skills_path.write_text(knowledge)
        print(f"      Generated product knowledge file: {skills_path}")
        logger.info(f"Product knowledge written to {skills_path}")

    def _build_config(
        self,
        product_info: dict,
        icp_info: dict,
        objections: dict,
        domain: str,
    ) -> dict:
        """Build the complete harvey.yaml config."""
        company = product_info.get("company_name", "Your Company")
        product = product_info.get("product_name", "Your Product")
        tone = product_info.get("tone", "professional, consultative, confident")

        return {
            "persona": {
                "name": "Harvey",
                "company": company,
                "role": "Business Development",
                "email": f"harvey@{domain}",
                "linkedin": f"linkedin.com/in/harvey-{domain.replace('.', '-')}",
                "tone": tone,
            },
            "product": {
                "name": product,
                "description": product_info.get("product_description", ""),
                "pricing": product_info.get("pricing", "Contact for pricing"),
                "key_benefits": product_info.get("key_benefits", []),
                "objection_responses": objections,
            },
            "icp": {
                "industries": icp_info.get("industries", ["Technology"]),
                "company_size": icp_info.get("company_size", "10-200 employees"),
                "titles": icp_info.get("titles", ["VP", "Director"]),
                "geography": icp_info.get("geography", ["United States"]),
            },
            "channels": {
                "email": {
                    "enabled": True,
                    "provider": "instantly",
                    "max_daily_sends": 50,
                },
                "linkedin": {
                    "enabled": True,
                    "max_daily_connections": 20,
                    "max_daily_messages": 10,
                },
            },
            "usage": {
                "max_daily_claude_percent": 80,
                "heartbeat_interval_minutes": 15,
                "quiet_hours": {
                    "start": "22:00",
                    "end": "07:00",
                    "timezone": "America/New_York",
                },
            },
        }


async def run_training(url: str, output: str = "harvey.yaml"):
    """Entry point for training."""
    trainer = Trainer()
    await trainer.train(url, output)


def main():
    """CLI entry point for training."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m harvey.trainer <website-url>")
        print("Example: python -m harvey.trainer https://acmecorp.com")
        sys.exit(1)

    url = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else "harvey.yaml"

    asyncio.run(run_training(url, output))


if __name__ == "__main__":
    main()
