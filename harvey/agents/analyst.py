"""Analyst — tracks performance and identifies what's working."""

import json
import logging
from datetime import datetime
from pathlib import Path

from harvey.state import StateManager

logger = logging.getLogger("harvey.analyst")


class Analyst:
    """Analyzes campaign performance, reply rates, and ICP segment conversion.

    Runs during idle cycles and writes a report to data/analytics.json.
    """

    def __init__(self, state: StateManager):
        self.state = state

    async def run(self):
        """Generate analytics report from current pipeline data."""
        logger.info("Analyst: Running performance analysis...")

        report = {
            "generated_at": datetime.utcnow().isoformat(),
            "pipeline": await self._pipeline_summary(),
            "campaigns": await self._campaign_performance(),
            "intents": await self._intent_breakdown(),
            "stages": await self._stage_breakdown(),
            "insights": [],
        }

        # Generate insights
        report["insights"] = self._generate_insights(report)

        # Write report
        data_dir = Path(self.state.db_path).parent
        report_path = data_dir / "analytics.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Analyst: Report written to {report_path}")

        # Log key metrics
        pipeline = report["pipeline"]
        total = sum(pipeline.values())
        if total > 0:
            reply_rate = pipeline.get("replied", 0) / max(pipeline.get("contacted", 0), 1) * 100
            logger.info(
                f"Analyst: {total} prospects | "
                f"{pipeline.get('contacted', 0)} contacted | "
                f"Reply rate: {reply_rate:.1f}%"
            )

        for insight in report["insights"]:
            logger.info(f"Analyst: Insight — {insight}")

    async def _pipeline_summary(self) -> dict:
        """Count prospects by status."""
        return await self.state.count_prospects_by_status()

    async def _campaign_performance(self) -> list[dict]:
        """Get stats for each campaign."""
        return await self.state.get_campaign_stats()

    async def _intent_breakdown(self) -> dict:
        """Count replies by intent classification."""
        return await self.state.get_intent_distribution()

    async def _stage_breakdown(self) -> dict:
        """Count conversations by sales stage."""
        return await self.state.get_stage_distribution()

    def _generate_insights(self, report: dict) -> list[str]:
        """Derive actionable insights from the data."""
        insights = []
        pipeline = report["pipeline"]
        intents = report["intents"]
        campaigns = report["campaigns"]

        # Pipeline health
        total_contacted = pipeline.get("contacted", 0)
        total_replied = pipeline.get("replied", 0)
        total_interested = intents.get("interested", 0)
        total_objections = intents.get("objection", 0)
        total_not_interested = intents.get("not_interested", 0)

        if total_contacted > 10:
            reply_rate = total_replied / total_contacted * 100
            if reply_rate < 5:
                insights.append(
                    f"Reply rate is low ({reply_rate:.1f}%). "
                    "Consider testing different subject lines or email frameworks."
                )
            elif reply_rate > 15:
                insights.append(
                    f"Reply rate is strong ({reply_rate:.1f}%). "
                    "Current messaging is resonating well."
                )

        # Objection patterns
        total_replies = sum(intents.values()) if intents else 0
        if total_replies > 5:
            objection_pct = total_objections / total_replies * 100
            if objection_pct > 40:
                insights.append(
                    f"{objection_pct:.0f}% of replies are objections. "
                    "Review objection handling skills or adjust targeting."
                )

        # Interest conversion
        if total_interested > 0 and total_contacted > 0:
            interest_rate = total_interested / total_contacted * 100
            insights.append(
                f"Interest rate: {interest_rate:.1f}% of contacted prospects show interest."
            )

        # Campaign comparison
        if len(campaigns) >= 2:
            best = max(campaigns, key=lambda c: c.get("reply_rate", 0))
            if best.get("reply_rate", 0) > 0:
                insights.append(
                    f"Best performing campaign: '{best['name']}' "
                    f"with {best['reply_rate']}% reply rate."
                )

        # Pipeline bottleneck
        stages = report["stages"]
        if stages.get("engaged", 0) > 3 and stages.get("closing", 0) == 0:
            insights.append(
                "Prospects are engaging but none are reaching the closing stage. "
                "The handler may need to be more assertive about proposing meetings."
            )

        if not insights:
            insights.append("Not enough data yet. Keep prospecting and sending campaigns.")

        return insights
