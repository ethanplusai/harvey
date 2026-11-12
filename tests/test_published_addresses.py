"""channels.email.published_only and trust_published: addresses a company prints itself.

Scout used to build name.surname@ from a pattern and verify the guess. With
published_only on it never deduces an address and skips Hunter. With
trust_published on, an address the company printed itself that no verifier
could decide (none configured, or its credits ran out) is graded "published"
instead of "guess". The two are independent, a verifier's "invalid" still
wins, and both are off by default: nothing here changes until one is turned on.
"""

from types import SimpleNamespace

import pytest

import sellbird.agents.scout as scout_mod
import sellbird.integrations.email_finder as ef
from sellbird.agents.scout import Scout
from sellbird.agents.sender import SENDABLE_EMAIL_STATUSES, SENDABLE_EMAIL_STATUSES_WITH_RISKY
from sellbird.config import EmailChannelConfig
from sellbird.gate import pre_send_check
from sellbird.state import WRITABLE_EMAIL_STATUSES

DOMAIN = "bakery.example"
KNOWN = [("jane.doe@bakery.example", "Jane", "Doe")]


@pytest.fixture(autouse=True)
def network_free(monkeypatch):
    async def mx(domain):
        return "mx.bakery.example"

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "classify_mx", lambda host: "other")


def verdict(monkeypatch, status):
    async def verify(email, mx_type, env):
        return status, False

    monkeypatch.setattr(ef, "_verify_candidate", verify)


# ── the setting ──


def test_both_options_are_off_by_default_and_independent():
    off = EmailChannelConfig()
    assert off.published_only is False and off.trust_published is False
    only = EmailChannelConfig(published_only=True)
    assert only.published_only is True and only.trust_published is False
    trust = EmailChannelConfig(trust_published=True)
    assert trust.published_only is False and trust.trust_published is True


# ── find_email ──


@pytest.mark.asyncio
async def test_without_the_option_a_published_address_nobody_could_decide_stays_a_guess(monkeypatch):
    verdict(monkeypatch, None)
    result = await ef.find_email("Jane", "Doe", DOMAIN, known_emails=KNOWN)
    assert (result.email, result.status) == ("jane.doe@bakery.example", "guess")


@pytest.mark.asyncio
async def test_trusting_published_addresses_makes_it_published(monkeypatch):
    verdict(monkeypatch, None)
    result = await ef.find_email("Jane", "Doe", DOMAIN, known_emails=KNOWN, trust_published=True)
    assert (result.email, result.status) == ("jane.doe@bakery.example", "published")


@pytest.mark.asyncio
async def test_published_only_alone_does_not_trust_an_undecided_address(monkeypatch):
    verdict(monkeypatch, None)
    result = await ef.find_email("Jane", "Doe", DOMAIN, known_emails=KNOWN, published_only=True)
    assert (result.email, result.status) == ("jane.doe@bakery.example", "guess")


@pytest.mark.asyncio
async def test_with_no_verification_asked_a_trusted_address_is_published(monkeypatch):
    result = await ef.find_email("Jane", "Doe", DOMAIN, known_emails=KNOWN,
                                 verify=False, trust_published=True)
    assert result.status == "published"


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier, expected", [
    ("verified", "verified"), ("risky", "risky"), ("invalid", "invalid")])
async def test_a_verifier_that_decides_still_wins(monkeypatch, verifier, expected):
    verdict(monkeypatch, verifier)
    result = await ef.find_email("Jane", "Doe", DOMAIN, known_emails=KNOWN,
                                 published_only=True, trust_published=True)
    assert result.status == expected


@pytest.mark.asyncio
async def test_with_published_only_nothing_is_deduced_when_the_site_names_no_address(monkeypatch):
    async def no_lookup(*a, **kw):
        raise AssertionError("no pattern lookup should be spent")

    monkeypatch.setattr(ef, "derive_pattern", no_lookup)
    verdict(monkeypatch, "verified")
    result = await ef.find_email("Jane", "Doe", DOMAIN, known_emails=[],
                                 published_only=True, trust_published=True)
    assert result.email == "" and result.status == "guess"


@pytest.mark.asyncio
async def test_with_the_option_someone_elses_published_address_is_not_borrowed(monkeypatch):
    verdict(monkeypatch, "verified")
    other = [("bob.roe@bakery.example", "Bob", "Roe")]
    result = await ef.find_email("Jane", "Doe", DOMAIN, known_emails=other, published_only=True)
    assert result.email == ""


@pytest.mark.asyncio
async def test_without_the_option_an_address_is_still_deduced(monkeypatch):
    verdict(monkeypatch, "verified")
    result = await ef.find_email("Jane", "Doe", DOMAIN, known_emails=[], verify=True)
    assert result.email.endswith("@bakery.example") and result.status == "verified"


# ── verify_published_address ──


@pytest.mark.asyncio
@pytest.mark.parametrize("flag, expected", [(False, "guess"), (True, "published")])
async def test_an_undecided_inbox_is_graded_by_trust_published(monkeypatch, flag, expected):
    verdict(monkeypatch, None)
    assert await ef.verify_published_address("info@bakery.example", trust_published=flag) == expected


@pytest.mark.asyncio
async def test_a_verifier_invalid_beats_published(monkeypatch):
    verdict(monkeypatch, "invalid")
    assert await ef.verify_published_address("info@bakery.example", trust_published=True) == "invalid"


@pytest.mark.asyncio
async def test_an_address_with_no_mail_server_is_invalid_even_if_published(monkeypatch):
    async def none(domain):
        return None

    monkeypatch.setattr(ef, "get_mx_host", none)
    assert await ef.verify_published_address("info@bakery.example", trust_published=True) == "invalid"


# ── who may be written to and sent to ──


def test_published_is_written_to_and_sent_to_but_a_guess_is_not():
    assert "published" in WRITABLE_EMAIL_STATUSES and "guess" not in WRITABLE_EMAIL_STATUSES
    assert "published" in SENDABLE_EMAIL_STATUSES and "guess" not in SENDABLE_EMAIL_STATUSES
    assert "published" in SENDABLE_EMAIL_STATUSES_WITH_RISKY


@pytest.mark.parametrize("status, passes", [
    ("published", True), ("verified", True), ("guess", False), ("invalid", False)])
def test_the_send_gate_lets_published_through(status, passes):
    prospect = SimpleNamespace(email="info@bakery.example", email_status=status)
    result = pre_send_check("info@bakery.example", "hello there", "A short, plain note. Question?",
                            prospect=prospect, kind="sequence")
    assert bool(result) is passes


# ── Scout ──


def scout(published_only=False, trust_published=False):
    s = Scout.__new__(Scout)
    s.config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
        published_only=published_only, trust_published=trust_published)))
    s.env = SimpleNamespace(hunter_api_key="a-key")
    s.state = None
    s._site_evidence = {}
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize("only, trust, expected", [
    (False, False, {}),
    (True, False, {"published_only": True}),
    (False, True, {"trust_published": True}),
    (True, True, {"published_only": True, "trust_published": True}),
])
async def test_scout_passes_each_option_to_the_finder_only_when_on(monkeypatch, only, trust, expected):
    seen = {}

    async def find(first, last, domain, **kw):
        seen.update(kw)
        return SimpleNamespace(email="jane@bakery.example", status="published")

    monkeypatch.setattr(scout_mod, "find_email", find)
    await scout(only, trust)._resolve_email("Jane", "Doe", DOMAIN)
    assert {k: v for k, v in seen.items() if k in ("published_only", "trust_published")} == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("trust, expected", [(False, {}), (True, {"trust_published": True})])
async def test_scout_grades_its_inbox_by_trust_published(monkeypatch, trust, expected):
    seen = {}

    async def grade(email, env=None, **kw):
        seen.update(kw)
        return "published"

    async def inbox(domain):
        return "info@bakery.example"

    async def not_dup(prospect):
        return False

    s = scout(False, trust)
    s._find_inbox_address = inbox
    s._is_duplicate_prospect = not_dup
    s._remember_prospect = lambda prospect: None
    monkeypatch.setattr(scout_mod, "verify_published_address", grade)
    await s._company_inbox_prospect("c1", DOMAIN, "Bakery", "Bakery")
    assert seen == expected


@pytest.mark.asyncio
async def test_scout_skips_hunter_with_published_only_on():
    assert await scout(True)._prospect_via_hunter() == 0

