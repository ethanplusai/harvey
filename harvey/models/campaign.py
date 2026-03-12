"""Campaign data model."""

import json
from datetime import datetime

from pydantic import BaseModel, Field


class EmailStep(BaseModel):
    step: int
    subject: str
    body: str
    delay_days: int = 0  # days after previous step


class Campaign(BaseModel):
    id: str
    name: str = ""
    channel: str = "email"  # email/linkedin
    instantly_campaign_id: str = ""
    sequence: list[EmailStep] = []
    status: str = "draft"  # draft/active/paused/completed
    prospect_ids: list[str] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def sequence_json(self) -> str:
        return json.dumps([s.model_dump() for s in self.sequence])

    @classmethod
    def sequence_from_json(cls, data: str) -> list[EmailStep]:
        return [EmailStep(**s) for s in json.loads(data)]
