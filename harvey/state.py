"""SQLite state manager. All of Harvey's memory lives here."""

import json
import uuid
from datetime import datetime, date
from pathlib import Path

import aiosqlite

from harvey.models.prospect import Prospect
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.conversation import Conversation, Message

DB_PATH = Path(__file__).parent.parent / "data" / "harvey.db"


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class StateManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(DB_PATH)

    async def init_db(self):
        """Create all tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS prospects (
                    id TEXT PRIMARY KEY,
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    linkedin_url TEXT DEFAULT '',
                    company TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    industry TEXT DEFAULT '',
                    company_size TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    status TEXT DEFAULT 'new',
                    score INTEGER DEFAULT 0,
                    personalization_notes TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY,
                    name TEXT DEFAULT '',
                    channel TEXT DEFAULT 'email',
                    instantly_campaign_id TEXT DEFAULT '',
                    sequence_json TEXT DEFAULT '[]',
                    prospect_ids_json TEXT DEFAULT '[]',
                    status TEXT DEFAULT 'draft',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    prospect_id TEXT REFERENCES prospects(id),
                    campaign_id TEXT DEFAULT '',
                    channel TEXT DEFAULT 'email',
                    thread_json TEXT DEFAULT '[]',
                    intent TEXT DEFAULT '',
                    status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY,
                    action_type TEXT,
                    agent TEXT,
                    details_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS usage_log (
                    id TEXT PRIMARY KEY,
                    date TEXT,
                    claude_calls INTEGER DEFAULT 0,
                    usage_percent REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
                CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
                CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
                CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(date);
            """)

    # ── Prospects ──

    async def add_prospect(self, prospect: Prospect) -> str:
        if not prospect.id:
            prospect.id = _new_id()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO prospects
                   (id, first_name, last_name, email, linkedin_url, company,
                    title, industry, company_size, source, status, score,
                    personalization_notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prospect.id, prospect.first_name, prospect.last_name,
                    prospect.email, prospect.linkedin_url, prospect.company,
                    prospect.title, prospect.industry, prospect.company_size,
                    prospect.source, prospect.status, prospect.score,
                    prospect.personalization_notes,
                    prospect.created_at.isoformat(),
                    prospect.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return prospect.id

    async def get_prospect(self, prospect_id: str) -> Prospect | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE id = ?", (prospect_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Prospect(**dict(row))

    async def get_prospects_by_status(self, status: str) -> list[Prospect]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [Prospect(**dict(r)) for r in rows]

    async def update_prospect_status(self, prospect_id: str, status: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                (status, datetime.utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    async def prospect_exists(self, email: str = "", linkedin_url: str = "") -> bool:
        """Check if a prospect already exists by email or LinkedIn URL."""
        async with aiosqlite.connect(self.db_path) as db:
            if email:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE email = ?", (email,)
                ) as cursor:
                    if await cursor.fetchone():
                        return True
            if linkedin_url:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE linkedin_url = ?", (linkedin_url,)
                ) as cursor:
                    if await cursor.fetchone():
                        return True
        return False

    async def count_prospects_by_status(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM prospects GROUP BY status"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    # ── Campaigns ──

    async def add_campaign(self, campaign: Campaign) -> str:
        if not campaign.id:
            campaign.id = _new_id()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO campaigns
                   (id, name, channel, instantly_campaign_id, sequence_json,
                    prospect_ids_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign.id, campaign.name, campaign.channel,
                    campaign.instantly_campaign_id, campaign.sequence_json(),
                    json.dumps(campaign.prospect_ids), campaign.status,
                    campaign.created_at.isoformat(),
                ),
            )
            await db.commit()
        return campaign.id

    async def get_campaigns_by_status(self, status: str) -> list[Campaign]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM campaigns WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                campaigns = []
                for r in rows:
                    d = dict(r)
                    d["sequence"] = Campaign.sequence_from_json(d.pop("sequence_json"))
                    d["prospect_ids"] = json.loads(d.pop("prospect_ids_json"))
                    campaigns.append(Campaign(**d))
                return campaigns

    async def update_campaign(self, campaign_id: str, **kwargs):
        async with aiosqlite.connect(self.db_path) as db:
            for key, value in kwargs.items():
                await db.execute(
                    f"UPDATE campaigns SET {key} = ? WHERE id = ?",
                    (value, campaign_id),
                )
            await db.commit()

    # ── Conversations ──

    async def add_conversation(self, convo: Conversation) -> str:
        if not convo.id:
            convo.id = _new_id()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO conversations
                   (id, prospect_id, campaign_id, channel, thread_json,
                    intent, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    convo.id, convo.prospect_id, convo.campaign_id,
                    convo.channel, convo.thread_json(), convo.intent,
                    convo.status, convo.created_at.isoformat(),
                    convo.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return convo.id

    async def get_conversations_by_status(self, status: str) -> list[Conversation]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM conversations WHERE status = ?", (status,)
            ) as cursor:
                rows = await cursor.fetchall()
                convos = []
                for r in rows:
                    d = dict(r)
                    d["thread"] = Conversation.thread_from_json(d.pop("thread_json"))
                    convos.append(Conversation(**d))
                return convos

    async def update_conversation(self, convo_id: str, **kwargs):
        async with aiosqlite.connect(self.db_path) as db:
            for key, value in kwargs.items():
                await db.execute(
                    f"UPDATE conversations SET {key} = ?, updated_at = ? WHERE id = ?",
                    (value, datetime.utcnow().isoformat(), convo_id),
                )
            await db.commit()

    # ── Actions Log ──

    async def log_action(self, action_type: str, agent: str, details: dict | None = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO actions (id, action_type, agent, details_json) VALUES (?, ?, ?, ?)",
                (_new_id(), action_type, agent, json.dumps(details or {})),
            )
            await db.commit()

    # ── Usage Tracking ──

    async def get_usage_today(self) -> int:
        today = date.today().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT claude_calls FROM usage_log WHERE date = ?", (today,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def increment_usage(self):
        today = date.today().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO usage_log (id, date, claude_calls)
                   VALUES (?, ?, 1)
                   ON CONFLICT(date) DO UPDATE SET claude_calls = claude_calls + 1""",
                (_new_id(), today),
            )
            await db.commit()

    # ── Summary for Decision Making ──

    async def get_state_summary(self) -> dict:
        """Get a high-level summary of current state for the decision engine."""
        prospect_counts = await self.count_prospects_by_status()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM campaigns WHERE status = 'draft'"
            ) as cursor:
                draft_campaigns = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM campaigns WHERE status = 'active'"
            ) as cursor:
                active_campaigns = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM conversations WHERE status = 'open'"
            ) as cursor:
                open_conversations = (await cursor.fetchone())[0]

        return {
            "prospects": prospect_counts,
            "draft_campaigns": draft_campaigns,
            "active_campaigns": active_campaigns,
            "open_conversations": open_conversations,
            "usage_today": await self.get_usage_today(),
        }
