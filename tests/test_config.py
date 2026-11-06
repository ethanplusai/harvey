"""Tests for configuration loading."""

import tempfile
import os

import yaml

from sellbird.config import SellbirdConfig, EnvConfig, load_env


def test_config_from_dict():
    """SellbirdConfig can be constructed from a dict (YAML-like)."""
    data = {
        "persona": {
            "name": "Sellbird",
            "company": "Acme",
            "role": "BDR",
            "email": "sellbird@acme.com",
            "linkedin": "",
            "tone": "professional",
        },
        "product": {
            "name": "Widget",
            "description": "A great widget",
            "pricing": "$99/mo",
            "key_benefits": ["Fast", "Reliable"],
            "objection_responses": {"too expensive": "Consider the ROI"},
        },
        "icp": {
            "industries": ["SaaS"],
            "company_size": "10-200",
            "titles": ["VP of Sales"],
            "geography": ["US"],
        },
    }
    config = SellbirdConfig(**data)
    assert config.persona.name == "Sellbird"
    assert config.product.name == "Widget"
    assert config.icp.industries == ["SaaS"]
    assert config.channels.email.max_daily_sends == 50  # default
    assert config.usage.heartbeat_interval_minutes == 15  # default


def test_env_config_defaults():
    """EnvConfig has sensible defaults when env vars are unset."""
    env = EnvConfig()
    assert env.instantly_api_key == ""
    assert env.serper_api_key == ""
    assert env.tinyfish_api_key == ""


def test_offer_config_defaults():
    """OfferConfig defaults to book_call via calendar_link."""
    data = {
        "persona": {"name": "H", "company": "A", "role": "B", "email": "e", "linkedin": "", "tone": "t"},
        "product": {"name": "P", "description": "D", "pricing": "$1", "key_benefits": [], "objection_responses": {}},
        "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
    }
    config = SellbirdConfig(**data)
    assert config.product.offer.goal == "book_call"
    assert config.product.offer.booking_method == "calendar_link"
    assert config.product.offer.meeting_duration == "15 minutes"
