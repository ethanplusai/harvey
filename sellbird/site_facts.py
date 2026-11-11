"""What Sellbird has already read off a company's own website, for the writer.

The free homepage signals (missing structured data, a site nobody has
touched in months, ...) sit in ``observations`` and show up
in cohorts and the dashboard, but the writer's prompt only carried the
company's description, detected tools and a few other signals. An email that
should name one concrete thing the sender would change had nothing true to
name, so it said it hadn't looked.

This turns the findings that are actually true into fact lines. Every line
says how it was found and that the check can miss things, so the writer
hedges instead of presenting a scan as an audit. A signal checked and found
absent is not a fact and is left out.

NO_ONLINE_BOOKING is deliberately not here. A booking widget that loads late,
or a "book" button on another page, is invisible to a read of the homepage
HTML: against real sites it called a bookable business unbookable in about
half of the cases checked, and an email telling an owner to add what they
already have is the worst first impression. It stays a signal for cohorts and
the dashboard, where a human reads it as a lead to check, not as a claim.
"""

# A site nobody has changed for this long reads as neglected; a few weeks doesn't.
STALE_DAYS = 180
# Pages in sitemap.xml at or below this make a very small site.
THIN_PAGES = 5

HEADER = (
    "- Site checks (an automated read of their public pages; it can miss things). "
    "Use at most ONE, only if it fits this kind of business, put it as what you "
    "would do in their place, and never call it an audit or say you went through "
    "the site:"
)


async def site_facts(state, company_id: str) -> list[str]:
    """Fact lines for the writer's prompt, or [] when nothing true is known.

    A failed read is just "nothing known", never a reason not to write.
    """
    if not company_id or state is None:
        return []
    lookup = getattr(state, "get_observations", None)
    if lookup is None:
        return []
    try:
        rows = await lookup(company_id=company_id, latest_only=True, limit=500)
    except Exception:
        return []
    current = {r.get("signal_code"): r.get("value_num") for r in rows}

    facts: list[str] = []
    if current.get("NO_SCHEMA_MARKUP") == 1:
        facts.append(
            "  - Their homepage has no JSON-LD structured data (schema.org markup), "
            "so search engines get less to show about them, such as opening hours, "
            "contact details or reviews")
    age = current.get("BLOG_STALE")
    if age is not None and age >= STALE_DAYS:
        facts.append(
            f"  - The newest date in their sitemap.xml is about {int(age)} days old")
    pages = current.get("SITE_PAGE_COUNT")
    if pages is not None and 0 < pages <= THIN_PAGES:
        facts.append(f"  - Their sitemap.xml lists only {int(pages)} page(s)")
    if current.get("BLOCKS_AI_CRAWLERS") == 1:
        facts.append(
            "  - Their robots.txt blocks AI crawlers, so AI search answers can't "
            "read their site")
    return [HEADER] + facts if facts else []
