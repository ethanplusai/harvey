"""Prospect data model."""

from datetime import datetime

from pydantic import BaseModel, Field


class Prospect(BaseModel):
    id: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    linkedin_url: str = ""
    company: str = ""
    title: str = ""
    industry: str = ""
    company_size: str = ""
    source: str = ""
    status: str = "new"  # new/contacted/replied/meeting/closed/lost
    score: int = 0
    personalization_notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()
