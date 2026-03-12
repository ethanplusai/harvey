"""Company data model."""

from datetime import datetime

from pydantic import BaseModel, Field


class Company(BaseModel):
    id: str = ""
    name: str = ""
    domain: str = ""
    website: str = ""
    description: str = ""
    industry: str = ""
    company_size: str = ""
    location: str = ""
    source: str = ""          # how we found them: google_dork, linkedin, company_scrape
    source_url: str = ""      # specific URL where we found the info
    notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
