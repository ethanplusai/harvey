"""Configuration loader for Harvey. Reads harvey.yaml + .env."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class PersonaConfig(BaseModel):
    name: str
    company: str
    role: str
    email: str
    linkedin: str
    tone: str


class OfferConfig(BaseModel):
    primary: str = ""
    entry: str = ""
    goal: str = "book_call"  # book_call, start_trial, get_reply
    booking_method: str = "calendar_link"  # calendar_link, suggest_times, ask_preference
    booking_url: str = ""
    meeting_duration: str = "15 minutes"
    meeting_owner: str = ""


class ProductConfig(BaseModel):
    name: str
    description: str
    pricing: str
    key_benefits: list[str]
    objection_responses: dict[str, str]
    offer: OfferConfig = OfferConfig()


class ICPConfig(BaseModel):
    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    provider: str = "instantly"
    max_daily_sends: int = 50


class LinkedInChannelConfig(BaseModel):
    enabled: bool = True
    max_daily_connections: int = 20
    max_daily_messages: int = 10


class ChannelsConfig(BaseModel):
    email: EmailChannelConfig = EmailChannelConfig()
    linkedin: LinkedInChannelConfig = LinkedInChannelConfig()


class QuietHoursConfig(BaseModel):
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "America/New_York"


class UsageConfig(BaseModel):
    max_daily_claude_percent: float = 80.0
    heartbeat_interval_minutes: int = 15
    quiet_hours: QuietHoursConfig = QuietHoursConfig()


class HarveyConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    usage: UsageConfig = UsageConfig()


class EnvConfig(BaseModel):
    instantly_api_key: str = ""
    linkedin_email: str = ""
    linkedin_password: str = ""
    hunter_api_key: str = ""


def load_config(config_path: str | None = None) -> HarveyConfig:
    """Load Harvey configuration from YAML file."""
    if config_path is None:
        config_path = _find_config_file()
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return HarveyConfig(**data)


def load_env() -> EnvConfig:
    """Load environment variables from .env file."""
    load_dotenv()
    return EnvConfig(
        instantly_api_key=os.getenv("INSTANTLY_API_KEY", ""),
        linkedin_email=os.getenv("LINKEDIN_EMAIL", ""),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", ""),
        hunter_api_key=os.getenv("HUNTER_API_KEY", ""),
    )


def _find_config_file() -> str:
    """Search for harvey.yaml in common locations."""
    candidates = [
        Path.cwd() / "harvey.yaml",
        Path.cwd().parent / "harvey.yaml",
        Path(__file__).parent.parent / "harvey.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise FileNotFoundError(
        "harvey.yaml not found. Create one from harvey.yaml.example"
    )
