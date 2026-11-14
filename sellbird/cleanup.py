"""Take the wrong companies out of the pipeline.

Sellbird discovers businesses by searching the web, and the web does not
return only businesses. Google ranks blog posts, directories, marketplaces
and listicles alongside real factories — so a search for "manufacturing
company Pune" turns up a page titled "Manufacturing Companies in Nagpur"
and the publisher of that page enters the pipeline as though it were a
prospect. The same search turns up ERP vendors and software houses, whose
blogs outrank factories for manufacturing keywords, and trading companies,
which Google ranks because they are the most common kind of Indian
business to have a website.

Those three kinds of company are not buyers. An ERP vendor already builds
an ERP. A trading house has no shopfloor to schedule. A blog post is not
a business at all. This module takes them out.

Nothing is deleted. A company is marked excluded, with the reason
recorded, and so are its prospects; any draft already waiting for approval
is cancelled. That way the operator can see exactly what was removed, and
put it back if the call was wrong — a trading company is not a prospect
today, but a distributor who buys a manufacturing ERP next year might be.

The same rules are applied live by ``sellbird.icp_filter`` at every point
where a company enters the pipeline. This module is the one-time sweep
over what was already there before those rules existed.
"""

import logging

import aiosqlite

logger = logging.getLogger("sellbird.cleanup")


async def audit(state, config=None) -> dict:
    """Which companies on file would the ICP filter reject?

    Read-only. Returns the counts and the companies themselves, so the
    operator can see what would be removed before anything is.
    """
    from sellbird.icp_filter import exclusion_reason

    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT c.id, c.name, c.domain, c.industry, c.description,
                      c.source_url, c.location, c.status,
                      (SELECT COUNT(*) FROM prospects p
                        WHERE p.company_id = c.id) AS prospects
               FROM companies c
               WHERE c.status != 'excluded'
               ORDER BY prospects DESC, c.name"""
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

    excluded: list[dict] = []
    for row in rows:
        reason = exclusion_reason(row)
        if reason:
            row["reason"] = reason
            excluded.append(row)

    by_reason: dict[str, int] = {}
    for row in excluded:
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1

    return {
        "total": len(rows),
        "excluded_count": len(excluded),
        "excluded": excluded,
        "by_reason": by_reason,
        "prospects_affected": sum(r["prospects"] for r in excluded),
    }


async def cleanup(state, config=None, dry_run: bool = False,
                  include_retail: bool = True) -> dict:
    """Exclude every company on file that the ICP filter rejects.

    ``dry_run`` reports what would happen and changes nothing.
    ``include_retail`` covers trading houses and retailers; turn it off to
    leave those alone and only take out vendors and content pages.
    """
    from sellbird.icp_filter import exclusion_reason

    report = await audit(state, config)
    excluded = report["excluded"]
    if not include_retail:
        excluded = [r for r in excluded if "trading" not in r["reason"]]

    totals = {"companies": 0, "prospects": 0, "cancelled_drafts": 0}
    by_reason: dict[str, int] = {}
    details: list[dict] = []

    for row in excluded:
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
        if dry_run:
            totals["companies"] += 1
            totals["prospects"] += row["prospects"]
            details.append({
                "name": row["name"],
                "reason": row["reason"],
                "prospects": row["prospects"],
            })
            continue
        result = await state.exclude_company(row["id"], row["reason"])
        totals["companies"] += 1
        totals["prospects"] += result["prospects"]
        totals["cancelled_drafts"] += result["cancelled_drafts"]
        details.append({
            "name": row["name"],
            "reason": row["reason"],
            "prospects": result["prospects"],
            "cancelled_drafts": result["cancelled_drafts"],
        })

    return {
        "dry_run": dry_run,
        "total_companies": report["total"],
        "excluded": totals["companies"],
        "prospects": totals["prospects"],
        "cancelled_drafts": totals["cancelled_drafts"],
        "by_reason": by_reason,
        "details": details,
    }
