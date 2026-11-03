"""Google Maps listings from a locally run gosom/google-maps-scraper binary.

Free, no API key, no per-search bill: the binary drives a headless browser
against Google Maps on this machine and writes one JSON record per business.
Set ``GMAPS_SCRAPER_PATH`` in ``.env`` to the downloaded executable.

Trade-offs, stated plainly: it is slower than an API (a few seconds a
business), it depends on Google's page layout staying put, and Google's terms
do not permit scraping Maps. Use it knowingly.
"""

import asyncio
import json
import logging
import math
import os
import tempfile

import httpx  # noqa: F401  (fetch() signature matches the other providers)

from harvey.collectors.discover import (
    Business, DiscoveryProvider, DiscoveryQuery, normalize_domain,
)

logger = logging.getLogger("harvey.collectors.google_maps_scraper")

# Records per scroll of the results panel, measured on a live run.
RESULTS_PER_SCROLL = 20
RUN_TIMEOUT = 900.0          # seconds for one query, hard cap
INACTIVITY = "2m"            # the binary exits once it stops finding results


def _clean(text: str) -> str:
    """One line per query: a newline in a term would start a second query."""
    return " ".join(str(text or "").split())


def build_query_line(query: DiscoveryQuery) -> str:
    return _clean(f"{query.term} in {query.location}" if query.location
                  else query.term)


def build_args(query: DiscoveryQuery, input_path: str, results_path: str) -> list[str]:
    """Command-line arguments for one query, from a Harvey DiscoveryQuery."""
    depth = max(1, math.ceil(query.limit / RESULTS_PER_SCROLL))
    args = [
        "-input", input_path,
        "-results", results_path,
        "-json",
        "-depth", str(depth),
        "-c", "1",
        "-lang", "en",
        "-exit-on-inactivity", INACTIVITY,
    ]
    if query.coordinate:
        lat, lng, radius_km = (query.coordinate.split(",") + ["", "", ""])[:3]
        args += ["-geo", f"{lat},{lng}"]
        if radius_km:
            args += ["-radius", str(int(float(radius_km) * 1000))]
    return args


def to_business(record: dict, query: DiscoveryQuery) -> Business | None:
    """Map one scraper record to a Business. None for records with no name."""
    name = _clean(record.get("title"))
    if not name:
        return None
    site = (record.get("web_site") or "").strip()
    cid = str(record.get("cid") or record.get("place_id") or "")
    return Business(
        name=name,
        raw_title=name,
        domain=normalize_domain(site),
        website=site,
        phone=(record.get("phone") or "").strip(),
        address=(record.get("address") or "").strip(),
        location=query.location,
        category=(record.get("category") or "").strip(),
        rating=record.get("review_rating") or None,
        review_count=record.get("review_count"),
        has_website=bool(site),
        external_id=f"gmaps:{cid}" if cid else "",
        source_url=record.get("link") or "",
        provider=GoogleMapsScraper.key,
    )


def parse_results(text: str, query: DiscoveryQuery) -> list[Business]:
    """Read the JSON-lines results file. Skips anything that isn't a record."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or "title" not in record:
            continue  # log lines and anything else the binary prints
        business = to_business(record, query)
        if business:
            out.append(business)
    return out


class GoogleMapsScraper(DiscoveryProvider):
    key = "gmaps_scraper"
    label = "Google Maps (local scraper)"
    kind = "listings"
    needs_coordinates = True
    blurb = ("Google Maps listings collected by a scraper running on this "
             "computer. Free per search, and it returns the businesses that "
             "have no website, which is where local trades are missed.")
    cost_note = "free (your computer and time)"
    free_tier = "no limit imposed by a vendor"
    signup_url = "https://github.com/gosom/google-maps-scraper/releases"
    env_keys = ("GMAPS_SCRAPER_PATH",)
    caveat = ("Slower than an API, and it depends on Google's page layout. "
              "Google's terms do not permit scraping Maps.")

    async def fetch(self, client, env, query):
        exe = env.get("GMAPS_SCRAPER_PATH", "")
        if not os.path.isfile(exe):
            raise RuntimeError(
                f"GMAPS_SCRAPER_PATH points to {exe!r}, which is not a file. "
                "Download the Windows build from the scraper's releases page "
                "and set the full path in .env.")

        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "queries.txt")
            results_path = os.path.join(tmp, "results.json")
            with open(input_path, "w", encoding="utf-8") as f:
                f.write(build_query_line(query) + "\n")

            args = build_args(query, input_path, results_path)
            proc = await asyncio.create_subprocess_exec(
                exe, *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(),
                                                   timeout=RUN_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(
                    f"The Google Maps scraper ran over {RUN_TIMEOUT:.0f}s on "
                    f"{query.keyword()!r} and was stopped.")

            if proc.returncode != 0:
                tail = (stderr or b"").decode("utf-8", "replace")[-300:]
                raise RuntimeError(
                    f"The Google Maps scraper exited with code "
                    f"{proc.returncode}: {tail.strip()}")

            if not os.path.exists(results_path):
                # A query with no results writes no file; that is an answer.
                return []
            with open(results_path, encoding="utf-8") as f:
                return parse_results(f.read(), query)
