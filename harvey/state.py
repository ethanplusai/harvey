"""SQLite state manager. All of Harvey's memory lives here."""

import json
import uuid
from datetime import datetime, date
from pathlib import Path

import aiosqlite

from harvey.models.company import Company
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
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS companies (
                    id TEXT PRIMARY KEY,
                    name TEXT DEFAULT '',
                    domain TEXT DEFAULT '',
                    website TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    industry TEXT DEFAULT '',
                    company_size TEXT DEFAULT '',
                    location TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    source_url TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS prospects (
                    id TEXT PRIMARY KEY,
                    company_id TEXT DEFAULT '' REFERENCES companies(id),
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    email_verified INTEGER DEFAULT 0,
                    phone TEXT DEFAULT '',
                    phone_verified INTEGER DEFAULT 0,
                    linkedin_url TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    seniority TEXT DEFAULT '',
                    department TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    source_url TEXT DEFAULT '',
                    status TEXT DEFAULT 'new',
                    score INTEGER DEFAULT 0,
                    personalization_notes TEXT DEFAULT '',
                    company TEXT DEFAULT '',
                    industry TEXT DEFAULT '',
                    company_size TEXT DEFAULT '',
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
                    stage TEXT DEFAULT 'initial_outreach',
                    status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT DEFAULT '',
                    entity_id TEXT DEFAULT '',
                    comment TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    date TEXT UNIQUE,
                    claude_calls INTEGER DEFAULT 0,
                    usage_percent REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS processed_replies (
                    reply_id TEXT PRIMARY KEY,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
                CREATE INDEX IF NOT EXISTS idx_prospects_company_id ON prospects(company_id);
                CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
                CREATE INDEX IF NOT EXISTS idx_prospects_email ON prospects(email);
                CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
                CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
                CREATE INDEX IF NOT EXISTS idx_feedback_entity ON feedback(entity_type, entity_id);
                CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(date);
            """)

    # ── Companies ──

    async def add_company(self, company: Company) -> str:
        if not company.id:
            company.id = _new_id()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO companies
                   (id, name, domain, website, description, industry,
                    company_size, location, source, source_url, notes,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    company.id, company.name, company.domain, company.website,
                    company.description, company.industry, company.company_size,
                    company.location, company.source, company.source_url,
                    company.notes,
                    company.created_at.isoformat(),
                    company.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return company.id

    async def get_company(self, company_id: str) -> Company | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE id = ?", (company_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Company(**dict(row))

    async def get_company_by_domain(self, domain: str) -> Company | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM companies WHERE domain = ?", (domain,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Company(**dict(row))

    async def get_contacts_for_company(self, company_id: str) -> list[Prospect]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
                (company_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [Prospect(**dict(r)) for r in rows]

    async def company_exists(self, domain: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM companies WHERE domain = ?", (domain,)
            ) as cursor:
                return bool(await cursor.fetchone())

    # ── Prospects (Contacts) ──

    async def add_prospect(self, prospect: Prospect) -> str:
        if not prospect.id:
            prospect.id = _new_id()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO prospects
                   (id, company_id, first_name, last_name, email, email_verified,
                    phone, phone_verified, linkedin_url, title, seniority,
                    department, source, source_url, status, score,
                    personalization_notes, company, industry, company_size,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prospect.id, prospect.company_id,
                    prospect.first_name, prospect.last_name,
                    prospect.email, int(prospect.email_verified),
                    prospect.phone, int(prospect.phone_verified),
                    prospect.linkedin_url, prospect.title,
                    prospect.seniority, prospect.department,
                    prospect.source, prospect.source_url,
                    prospect.status, prospect.score,
                    prospect.personalization_notes,
                    prospect.company, prospect.industry, prospect.company_size,
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
                d = dict(row)
                d["email_verified"] = bool(d.get("email_verified", 0))
                d["phone_verified"] = bool(d.get("phone_verified", 0))
                return Prospect(**d)

    async def get_prospects_by_status(self, status: str) -> list[Prospect]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    d = dict(r)
                    d["email_verified"] = bool(d.get("email_verified", 0))
                    d["phone_verified"] = bool(d.get("phone_verified", 0))
                    results.append(Prospect(**d))
                return results

    async def update_prospect_status(self, prospect_id: str, status: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE prospects SET status = ?, updated_at = ? WHERE id = ?",
                (status, datetime.utcnow().isoformat(), prospect_id),
            )
            await db.commit()

    async def get_prospect_by_email(self, email: str) -> "Prospect | None":
        """Look up a prospect by email address (indexed query)."""
        if not email:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM prospects WHERE email = ?", (email,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["email_verified"] = bool(d.get("email_verified", 0))
                d["phone_verified"] = bool(d.get("phone_verified", 0))
                return Prospect(**d)

    async def prospect_exists(
        self, email: str = "", linkedin_url: str = "",
        first_name: str = "", last_name: str = "", company: str = "",
    ) -> bool:
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
            # Name + company dedup (case-insensitive)
            if first_name and last_name and company:
                async with db.execute(
                    "SELECT 1 FROM prospects WHERE LOWER(first_name) = ? AND LOWER(last_name) = ? AND LOWER(company) = ?",
                    (first_name.lower(), last_name.lower(), company.lower()),
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

    # ── Feedback ──

    async def add_feedback(
        self, entity_type: str, entity_id: str, comment: str
    ) -> str:
        feedback_id = _new_id()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO feedback (id, entity_type, entity_id, comment)
                   VALUES (?, ?, ?, ?)""",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
        return feedback_id

    async def get_feedback(
        self, entity_type: str, entity_id: str
    ) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM feedback
                   WHERE entity_type = ? AND entity_id = ?
                   ORDER BY created_at DESC""",
                (entity_type, entity_id),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_feedback(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC LIMIT 100"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # ── Reply Deduplication ──

    async def is_reply_processed(self, reply_id: str) -> bool:
        """Check if a reply has already been processed."""
        if not reply_id:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM processed_replies WHERE reply_id = ?", (reply_id,)
            ) as cursor:
                return bool(await cursor.fetchone())

    async def mark_reply_processed(self, reply_id: str):
        """Mark a reply as processed to avoid double-handling."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO processed_replies (reply_id) VALUES (?)",
                (reply_id,),
            )
            await db.commit()

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
                    intent, stage, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    convo.id, convo.prospect_id, convo.campaign_id,
                    convo.channel, convo.thread_json(), convo.intent,
                    convo.stage, convo.status, convo.created_at.isoformat(),
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

    # ── Analytics ──

    async def get_campaign_stats(self) -> list[dict]:
        """Get performance stats for each campaign."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT c.id, c.name, c.status, c.prospect_ids_json,
                          (SELECT COUNT(*) FROM conversations
                           WHERE campaign_id = c.id) as reply_count,
                          (SELECT COUNT(*) FROM conversations
                           WHERE campaign_id = c.id AND intent = 'interested') as interested_count,
                          (SELECT COUNT(*) FROM conversations
                           WHERE campaign_id = c.id AND intent = 'objection') as objection_count,
                          (SELECT COUNT(*) FROM conversations
                           WHERE campaign_id = c.id AND intent = 'not_interested') as not_interested_count
                   FROM campaigns c
                   WHERE c.status IN ('active', 'completed')
                   ORDER BY c.created_at DESC"""
            ) as cursor:
                rows = await cursor.fetchall()
                stats = []
                for r in rows:
                    d = dict(r)
                    prospect_ids = json.loads(d.get("prospect_ids_json", "[]"))
                    d["leads_count"] = len(prospect_ids)
                    d["reply_rate"] = (
                        round(d["reply_count"] / len(prospect_ids) * 100, 1)
                        if prospect_ids else 0
                    )
                    stats.append(d)
                return stats

    async def get_intent_distribution(self) -> dict[str, int]:
        """Count conversations by intent."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT intent, COUNT(*) FROM conversations WHERE intent != '' GROUP BY intent"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_stage_distribution(self) -> dict[str, int]:
        """Count conversations by sales stage."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT stage, COUNT(*) FROM conversations WHERE stage != '' GROUP BY stage"
            ) as cursor:
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

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
