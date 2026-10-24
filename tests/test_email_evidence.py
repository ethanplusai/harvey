"""What a site's published addresses say about its email format, and a
pattern cache that no longer gets stuck on a default guess."""

import pytest

import harvey.integrations.email_finder as ef


# ── email patterns from published addresses ──

@pytest.mark.parametrize("local, pattern", [
    ("j.smith", "{fi}.{l}"),
    ("john.smith", "{f}.{l}"),
    ("john_smith", "{f}_{l}"),
])
def test_pattern_from_an_untied_address(local, pattern):
    assert ef.pattern_from_local(local)[0] == pattern


@pytest.mark.parametrize("local", [
    "info", "office", "servicesubmissions", "scholarships", "web2", "x",
    # A lone dotless address is ambiguous: jadams@ could be Jadams or J. Adams,
    # sven@ a first name or S. Ven. Nobody on the site to tell, nothing learned.
    "brent", "mandie", "jsmith", "jadams", "jallen", "aowens", "eolsen", "sven", "drsmith",
])
def test_role_or_junk_addresses_teach_nothing(local):
    assert ef.pattern_from_local(local) is None


def test_an_address_tied_to_a_named_person_beats_guessing():
    known, pattern, conf = ef.site_email_evidence(
        ["tbrown@acme.com", "info@acme.com", "dana@other.com"],
        [("Tim", "Brown")], "acme.com")
    assert known == [("tbrown@acme.com", "Tim", "Brown")]
    assert pattern == "{fi}{l}" and conf >= 0.9


def test_untied_dotless_addresses_teach_nothing():
    known, pattern, conf = ef.site_email_evidence(
        ["brynn@hookagency.com", "mandie@hookagency.com", "jadams@hookagency.com"],
        [("Tim", "Brown")], "hookagency.com")
    assert (known, pattern) == ([], "")


def test_a_named_surname_disambiguates_an_untied_address():
    # Nobody named Jim Smith, but Amy Smith works there: jsmith@ is an
    # initial plus a surname, not a first name.
    known, pattern, conf = ef.site_email_evidence(
        ["jsmith@acme.com"], [("Amy", "Smith"), ("Tim", "Brown")], "acme.com")
    assert known == [] and pattern == "{fi}{l}" and conf < 0.9


@pytest.mark.parametrize("address, pattern", [("jsmith@acme.com", "{fi}{l}"), ("smith@acme.com", "{l}")])
def test_an_address_two_named_people_could_own_is_format_evidence_only(address, pattern):
    known, learned, _ = ef.site_email_evidence(
        [address], [("John", "Smith"), ("Jane", "Smith")], "acme.com")
    assert known == [] and learned == pattern


@pytest.mark.asyncio
async def test_never_guess_an_address_another_named_person_could_own(monkeypatch):
    async def mx(domain):
        return "aspmx.l.google.com"

    async def verified(email, mx_type, env):
        raise AssertionError("a colliding address must never be sent to a verifier")

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_verify_candidate", verified)
    st = PatternState({"pattern": "{fi}{l}", "source": "site_address", "confidence": 0.9})
    result = await ef.find_email("Jane", "Smith", "acme.com", state=st,
                                 other_people=[("John", "Smith")])
    assert result.email == "" and result.status != "verified"
    st = PatternState({"pattern": "{l}", "source": "site_address", "confidence": 0.9})
    result = await ef.find_email("Jane", "Smith", "acme.com", state=st,
                                 other_people=[("John", "Smith")])
    assert result.email == ""


@pytest.mark.asyncio
async def test_another_persons_published_address_is_never_this_persons_guess(monkeypatch):
    async def mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", mx)
    st = PatternState({"pattern": "{f}", "source": "site_address", "confidence": 0.9})
    result = await ef.find_email(
        "Tim", "Wu", "acme.com", verify=False, state=st, other_people=[("Tim", "Brown")],
        known_emails=[("tim@acme.com", "Tim", "Brown")])
    assert result.email == ""


# ── pattern derivation no longer gets stuck on a default ──

class PatternState:
    def __init__(self, cached=None):
        self.cached = cached
        self.saved = []

    async def get_email_pattern(self, domain):
        return self.cached

    async def save_email_pattern(self, domain, pattern="", source="", confidence=0.0, **kw):
        self.saved.append((pattern, source, confidence))

    async def get_setting(self, key, default=None):
        return "0"

    async def increment_setting(self, key):
        pass


@pytest.mark.asyncio
async def test_a_cached_default_does_not_block_a_known_address():
    st = PatternState({"pattern": "{f}.{l}", "source": "default", "confidence": 0.3})
    pattern, source, _ = await ef.derive_pattern(
        "acme.com", known_emails=[("tbrown@acme.com", "Tim", "Brown")], state=st)
    assert (pattern, source) == ("{fi}{l}", "known_email")


@pytest.mark.asyncio
async def test_a_cached_default_does_not_block_hunter(monkeypatch):
    calls = []

    async def hunter(domain, key):
        calls.append(domain)
        return "{fi}{l}", 0.8

    monkeypatch.setattr(ef, "_hunter_domain_pattern", hunter)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "k")
    st = PatternState({"pattern": "{f}.{l}", "source": "default", "confidence": 0.3})
    assert (await ef.derive_pattern("acme.com", state=st))[0] == "{fi}{l}"
    assert calls == ["acme.com"]


@pytest.mark.asyncio
async def test_hunter_is_not_asked_twice_about_a_domain_it_had_no_pattern_for(monkeypatch):
    calls = []

    async def hunter(domain, key):
        calls.append(domain)
        return None

    monkeypatch.setattr(ef, "_hunter_domain_pattern", hunter)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "k")
    first = await ef.derive_pattern("acme.com", state=PatternState())
    assert first[1] == "default_after_hunter" and calls == ["acme.com"]
    st = PatternState({"pattern": first[0], "source": first[1], "confidence": first[2]})
    await ef.derive_pattern("acme.com", state=st)
    assert calls == ["acme.com"]


@pytest.mark.asyncio
async def test_a_low_confidence_site_guess_still_gets_one_hunter_lookup(monkeypatch):
    calls = []

    async def hunter(domain, key):
        calls.append(domain)
        return None

    monkeypatch.setattr(ef, "_hunter_domain_pattern", hunter)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "k")
    cached = {"pattern": "{fi}{l}", "source": "site_address_guess", "confidence": 0.6}
    pattern, source, conf = await ef.derive_pattern("acme.com", state=PatternState(cached))
    assert calls == ["acme.com"] and pattern == "{fi}{l}" and source.endswith("_after_hunter")
    await ef.derive_pattern("acme.com", state=PatternState(
        {"pattern": pattern, "source": source, "confidence": conf}))
    assert calls == ["acme.com"]


@pytest.mark.asyncio
async def test_a_hunter_outage_is_not_remembered_as_no_pattern(monkeypatch):
    async def down(domain, key):
        raise ef.HunterUnavailable("429")

    monkeypatch.setattr(ef, "_hunter_domain_pattern", down)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "k")
    _, source, _ = await ef.derive_pattern("acme.com", state=PatternState())
    assert source == "default"      # asked again next time


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.asyncio
async def test_hunter_errors_are_transient(monkeypatch, status):
    class Resp:
        status_code = status

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return Resp()

    monkeypatch.setattr(ef.httpx, "AsyncClient", Client)
    with pytest.raises(ef.HunterUnavailable):
        await ef._hunter_domain_pattern("acme.com", "k")


@pytest.mark.asyncio
async def test_a_small_business_with_no_evidence_defaults_to_first_name(monkeypatch):
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    pattern, source, conf = await ef.derive_pattern(
        "acme.com", state=PatternState(), default_pattern=ef.SMALL_BUSINESS_PATTERN)
    assert (pattern, source) == ("{f}", "default") and conf < 0.5


@pytest.mark.asyncio
async def test_a_persons_own_published_address_is_used_as_is(monkeypatch):
    async def mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", mx)
    result = await ef.find_email(
        "Beth", "Hardy", "hardymarketingco.com", verify=False, state=PatternState(),
        known_emails=[("beth@hardymarketingco.com", "Beth", "Hardy")])
    assert result.email == "beth@hardymarketingco.com"
    assert result.status == "guess"   # unverified stays a guess: never sent
