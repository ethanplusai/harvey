"""Contact data model — a person at a company."""

from datetime import datetime

from pydantic import BaseModel, Field


class Prospect(BaseModel):
    id: str = ""
    company_id: str = ""       # FK to companies table
    first_name: str = ""       # required for a valid contact
    last_name: str = ""        # required for a valid contact
    email: str = ""
    email_verified: bool = False
    phone: str = ""
    phone_verified: bool = False
    linkedin_url: str = ""
    title: str = ""            # required for a valid contact
    seniority: str = ""        # c_suite, vp, director, manager, individual
    department: str = ""
    source: str = ""           # how we found them
    source_url: str = ""       # where we found the info
    status: str = "new"        # new/contacted/replied/meeting/closed/lost
    score: int = 0
    personalization_notes: str = ""
    # legacy fields kept for backwards compat with existing code
    company: str = ""
    industry: str = ""
    company_size: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def is_valid(self) -> bool:
        """A contact must have at minimum a name and title."""
        return bool(self.first_name and self.last_name and self.title)
