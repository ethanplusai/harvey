"""Company data model."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    """Naive UTC now (consistent with DB storage; avoids deprecated utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Company(BaseModel):
    id: str = ""
    name: str = ""
    domain: str = ""
    website: str = ""
    description: str = ""
    industry: str = ""
    company_size: str = ""
    location: str = ""
    phone: str = ""
    source: str = ""          # how we found them: google_dork, linkedin, company_scrape
    source_url: str = ""      # specific URL where we found the info
    # Provider-stable identity ("dataforseo:ChIJ...", "osm:node/123"). The
    # dedup key for businesses that have no website — which are precisely the
    # best prospects for anyone selling one.
    external_id: str = ""
    notes: str = ""
    # Buying signals: detected tools on their site, and timely triggers
    # (hiring, funding, ...) as [{"type": ..., "detail": ..., "found_at": ...}]
    tech_stack: list[str] = Field(default_factory=list)
    signals: list[dict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        """Domains are dedup keys — normalize case/whitespace."""
        return (v or "").strip().lower()
