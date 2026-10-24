"""Scout end to end on a fake site: it follows the site's own menu to the
owner, uses published addresses, falls back to the company inbox when the
owner's address is only a guess, and reports bot challenges as such."""

from types import SimpleNamespace

import pytest

import harvey.agents.scout as scout_mod
import harvey.integrations.email_finder as ef
from harvey.agents.scout import Scout
from harvey.collectors.profile import ProfileCollector
from tests.test_people_finder import HOOK, MARTENS

OWNER_ICP = ["Owner", "Founder", "President", "CEO"]


# ── Scout end to end, on a fake site ──

class FakeState:
    def __init__(self):
        self.patterns = {}
        self.observations = []
        self.confirmed = set()

    async def get_email_pattern(self, domain):
        return self.patterns.get(domain)

    async def save_email_pattern(self, domain, pattern="", source="", confidence=0.0, **kw):
        cur = self.patterns.get(domain)
        if pattern and (not cur or confidence >= cur["confidence"]):
            self.patterns[domain] = {"pattern": pattern, "source": source, "confidence": confidence}

    async def get_setting(self, key, default=None):
        return "0"

    async def increment_setting(self, key):
        pass

    async def get_contacts_for_company(self, company_id):
        return []

    async def prospect_exists(self, **kw):
        return False

    async def confirmed_signal_codes(self):
        return self.confirmed

    async def add_observations(self, rows, run_id=""):
        self.observations.extend(rows)
        return len(rows)


def site_scout(pages, titles=OWNER_ICP, verdict=None, monkeypatch=None):
    """A real Scout whose network is a dict of url -> html (or a response)."""
    config = SimpleNamespace(
        icp=SimpleNamespace(titles=list(titles), industries=["Roofing"], geography=[]),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)))
    scout = Scout(brain=None, state=FakeState(), config=config, env=SimpleNamespace())
    scout.fetched = []

    async def fetch(url, **kw):
        scout.fetched.append(url)
        page = pages.get(url)
        if page is None:
            return None
        if isinstance(page, SimpleNamespace):
            return page
        return SimpleNamespace(text=page, url=url, status_code=200)

    scout._fetch = fetch
    return scout


@pytest.fixture
def no_network(monkeypatch):
    async def mx(domain):
        return "aspmx.l.google.com"

    async def boom(*a, **k):
        raise AssertionError("no paid or verification call in tests")

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    for name in ("verify_reoon", "verify_zerobounce", "verify_hunter", "verify_email_smtp",
                 "_hunter_domain_pattern"):
        monkeypatch.setattr(ef, name, boom)

    async def unverified(email, env=None):
        return "guess"

    monkeypatch.setattr(scout_mod, "verify_published_address", unverified)


@pytest.mark.asyncio
async def test_scout_follows_the_menu_to_the_owner(no_network):
    pages = {
        "https://martensheating.com": '<a href="/about-us/">About Us</a><a href="/contact-us/">Contact</a>',
        "https://martensheating.com/about-us/": MARTENS,
        "https://martensheating.com/contact-us/": "<p>Call us</p>",
    }
    scout = site_scout(pages)
    members = await scout._scrape_team_page("martensheating.com")
    assert (members[0]["first_name"], members[0]["last_name"]) == ("Jason", "Martens")
    assert members[0]["owner"] is True
    # the old hardcoded guesses (/team, /our-team, /people...) are not tried
    # when the site's own menu says where its people are
    assert "https://martensheating.com/team" not in scout.fetched


@pytest.mark.asyncio
async def test_a_page_that_is_really_the_homepage_is_skipped(no_network):
    home = '<a href="/about/">About</a><h3>Pat Kim</h3><p>Owner</p>'
    pages = {
        "https://acme.com": home,
        "https://acme.com/about/": SimpleNamespace(text=home, url="https://acme.com/", status_code=200),
    }
    scout = site_scout(pages)
    site = await scout._read_site("acme.com")
    assert [url for url, _ in site["pages"]] == ["https://acme.com"]


@pytest.mark.asyncio
async def test_scout_reads_no_more_than_six_pages(no_network):
    links = "".join(f'<a href="/team/member-{i}/">Team member {i}</a>' for i in range(15))
    pages = {"https://acme.com": links}
    pages.update({f"https://acme.com/team/member-{i}/": f"<p>member {i}</p>" for i in range(15)})
    scout = site_scout(pages)
    site = await scout._read_site("acme.com")
    assert len(site["pages"]) <= 6
    assert len(scout.fetched) <= 9


@pytest.mark.asyncio
async def test_published_first_name_addresses_set_the_owners_guess(no_network):
    pages = {
        "https://hookagency.com": '<a href="/about-us/">About</a><a href="/contact-us/">Contact</a>',
        "https://hookagency.com/about-us/": HOOK,
        "https://hookagency.com/contact-us/":
            '<a href="mailto:matt@hookagency.com">Matt Anderson</a> <a href="mailto:brynn@hookagency.com">B</a>',
    }
    scout = site_scout(pages)
    contacts = await scout._contacts_from_company("c1", "hookagency.com", "Hook Agency", "Marketing")
    named = [p for p in contacts if not p.is_company_inbox]
    assert [(p.first_name, p.email) for p in named][:2] == [
        ("Tim", "tim@hookagency.com"), ("Bea", "bea@hookagency.com")]
    assert all(p.email_status == "guess" for p in named)   # unverified: never sent


@pytest.mark.asyncio
async def test_obfuscated_address_on_the_owners_card_is_theirs(no_network):
    pages = {
        "https://connellgelb.com": '<a href="/team/">Attorneys</a>',
        "https://connellgelb.com/team/": """
          <h2>Michelle D. Connell</h2><p>Attorney</p>
          <a class="tmf-email" data-front="mconnell" data-back="connellgelb.com"></a>
          <p>Michelle founded Connell &amp; Gelb with longtime colleague Wes Gelb.</p>
          <h2>Kate Barefoot</h2><p>Paralegal</p>""",
    }
    scout = site_scout(pages, titles=["Owner", "Partner"])
    contacts = await scout._contacts_from_company("c1", "connellgelb.com", "Connell & Gelb", "Law")
    assert [(p.first_name, p.email) for p in contacts if not p.is_company_inbox] == [
        ("Michelle", "mconnell@connellgelb.com")]


@pytest.mark.asyncio
async def test_inbox_is_added_when_the_owners_address_is_only_a_guess(no_network):
    pages = {
        "https://martensheating.com": '<a href="/about-us/">About</a>',
        "https://martensheating.com/about-us/": MARTENS + '<a href="mailto:office@martensheating.com">Email</a>',
    }
    scout = site_scout(pages)
    contacts = await scout._contacts_from_company("c1", "martensheating.com", "Martens", "HVAC")
    assert [p.is_company_inbox for p in contacts] == [False, True]
    assert contacts[0].email_status == "guess" and contacts[1].email == "office@martensheating.com"


@pytest.mark.asyncio
async def test_no_inbox_when_the_owner_has_a_deliverable_address(no_network, monkeypatch):
    async def verified(first, last, domain, **kw):
        return f"{first.lower()}@{domain}", "verified"

    pages = {
        "https://acme.com": '<h3>Pat Kim</h3><p>Owner</p><a href="mailto:info@acme.com">x</a>',
    }
    scout = site_scout(pages)
    scout._resolve_email = verified
    contacts = await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing")
    assert [p.is_company_inbox for p in contacts] == [False]


@pytest.mark.asyncio
async def test_inbox_deep_in_a_long_page_is_found(no_network):
    filler = "<p>We fix roofs.</p>" * 9000
    pages = {"https://integrityroofingandsiding.com":
             filler + '<a href="mailto:info@integrityroofingandsiding.com">Email</a>'}
    scout = site_scout(pages)
    assert await scout._find_inbox_address("integrityroofingandsiding.com") == \
        "info@integrityroofingandsiding.com"


@pytest.mark.asyncio
async def test_inbox_on_the_businesses_other_domain_is_allowed(no_network):
    pages = {
        "https://texastraditionsroofing.com": '<a href="mailto:admin@txtroofing.com">Email</a>',
        # txtroofing.com is theirs: it lands on their own site
        "https://txtroofing.com": SimpleNamespace(
            text="<title>Texas Traditions</title>", url="https://www.texastraditionsroofing.com/",
            status_code=200),
    }
    scout = site_scout(pages)
    assert await scout._find_inbox_address("texastraditionsroofing.com") == "admin@txtroofing.com"


@pytest.mark.asyncio
async def test_inbox_on_someone_elses_domain_is_not(no_network):
    pages = {
        "https://acme.com": '<a href="mailto:info@roofsupplier.com">Our supplier</a>',
        "https://roofsupplier.com": "<title>Roof Supplier</title>",
    }
    scout = site_scout(pages)
    assert await scout._find_inbox_address("acme.com") == ""


@pytest.mark.asyncio
async def test_likely_owner_is_observed_only_when_confirmed(no_network):
    pages = {"https://acme.com": "<h3>Pat Kim</h3><p>Owner</p>"}
    scout = site_scout(pages)
    await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing")
    assert scout.state.observations == []

    scout = site_scout(pages)
    scout.state.confirmed = {"LIKELY_OWNER"}
    await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing")
    [obs] = scout.state.observations
    assert obs["signal_code"] == "LIKELY_OWNER" and obs["company_id"] == "c1"
    assert obs["value_num"] == 1 and "Pat Kim" in obs["value_text"]
    assert obs["evidence_url"] == "https://acme.com"


@pytest.mark.asyncio
async def test_css_cards_are_only_a_fallback_and_reject_common_words(no_network):
    html = """<div class="team-member"><h3>Team of Experts</h3><p>Owner, CEO</p></div>
              <div class="team-member"><h4>Ana Ruiz</h4><span class="role">Owner</span></div>"""
    scout = site_scout({"https://acme.com": html})
    members = await scout._scrape_team_page("acme.com")
    assert [(m["first_name"], m["last_name"]) for m in members] == [("Ana", "Ruiz")]


# ── blocked or challenged, not unreachable ──

def test_a_recaptcha_script_is_not_a_block():
    page = ('<html><head><script src="https://www.google.com/recaptcha/api.js"></script></head>'
            '<body>' + "<p>Real roofing content.</p>" * 50 + '<div class="g-recaptcha"></div></body></html>')
    assert not Scout._looks_blocked(page)


@pytest.mark.parametrize("page", [
    "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>",
    '<html><head><title>Access to this page has been denied</title><meta name="description" content="px-captcha"></head></html>',
    "<html><body>Our systems have detected unusual traffic from your computer network.</body></html>",
])
def test_real_challenge_pages_are_blocks(page):
    assert Scout._looks_blocked(page)


SG_CHALLENGE = ('<html><head><link rel="icon" href="data:;"><meta http-equiv="refresh" '
                'content="0;/.well-known/sgcaptcha/?r=%2F&y=ipr:1.2.3.4:1.1"></meta></head></html>')


@pytest.mark.asyncio
async def test_siteground_challenge_is_reported_as_challenged_without_retrying(monkeypatch):
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            calls.append(url)
            return SimpleNamespace(status_code=202, text=SG_CHALLENGE, url=url)

    monkeypatch.setattr(scout_mod, "public_http_client", lambda **kw: Client())

    async def no_sleep(*a):
        pass

    monkeypatch.setattr(scout_mod.asyncio, "sleep", no_sleep)
    scout = site_scout({})
    del scout._fetch                     # use the real one
    base, resp = await scout._resolve_base_url("sixthgenroofing.com", retries=3)
    assert (base, resp) == ("", None)
    assert scout.challenged("sixthgenroofing.com")
    assert calls == ["https://sixthgenroofing.com"]   # one request, no retry loop
    assert await scout._scrape_team_page("sixthgenroofing.com") == []


@pytest.mark.asyncio
async def test_profile_collector_reports_a_challenge_without_trying_every_variant():
    calls = []

    class Client:
        async def get(self, url):
            calls.append(url)
            return SimpleNamespace(status_code=202, text=SG_CHALLENGE, headers={"content-type": "text/html"})

    collector = ProfileCollector(state=None)
    collector._client = Client()
    assert await collector._resolve_site("morneaulaw.com") == ("", "")
    assert calls == ["https://morneaulaw.com"]
    assert "morneaulaw.com" in collector.challenged


@pytest.mark.asyncio
async def test_no_size_signal_keeps_the_first_last_default(no_network):
    pages = {"https://bigcorp.com": "<h3>John Doe</h3><p>CEO</p>"}
    scout = site_scout(pages)
    [p, *_] = await scout._contacts_from_company("c1", "bigcorp.com", "BigCorp", "Software")
    assert p.email == "john.doe@bigcorp.com"


@pytest.mark.asyncio
async def test_a_small_icp_allows_the_first_name_default(no_network):
    pages = {"https://acme.com": "<h3>John Doe</h3><p>Owner</p>"}
    scout = site_scout(pages)
    scout.config.icp.company_size = "1-10 employees"
    [p, *_] = await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing")
    assert p.email == "john@acme.com"


@pytest.mark.asyncio
async def test_a_local_business_listing_allows_the_first_name_default(no_network):
    pages = {"https://acme.com": "<h3>John Doe</h3><p>Owner</p>"}
    scout = site_scout(pages)

    async def company(domain):
        return SimpleNamespace(id="c1", company_size="", source="discover:osm")

    scout.state.get_company_by_domain = company
    [p, *_] = await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing")
    assert p.email == "john@acme.com"


@pytest.mark.asyncio
async def test_two_people_who_could_share_an_address_get_no_guess(no_network):
    pages = {"https://acme.com": "<h3>John Smith</h3><p>Owner</p><h3>Jane Smith</h3><p>Owner</p>"
                                 '<a href="mailto:jsmith@acme.com">Email</a>'}
    scout = site_scout(pages)
    contacts = await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing")
    named = [p for p in contacts if not p.is_company_inbox]
    assert [p.email for p in named] == ["", ""]


@pytest.mark.asyncio
async def test_a_menu_link_that_redirects_off_site_is_not_read(no_network):
    pages = {
        "https://acme.com": '<a href="/team/">Team</a><a href="/about/">About</a>',
        "https://acme.com/team/": SimpleNamespace(
            text="<h3>Franchise Boss</h3><p>CEO</p>", url="https://bigfranchise.com/team/", status_code=200),
        "https://acme.com/about/": "<p>About us</p>",
    }
    scout = site_scout(pages)
    site = await scout._read_site("acme.com")
    assert [u for u, _ in site["pages"]] == ["https://acme.com", "https://acme.com/about/"]


@pytest.mark.asyncio
async def test_one_request_budget_covers_everything_scout_fetches_for_a_company(no_network):
    links = "".join(f'<a href="/team/m{i}/">Team {i}</a>' for i in range(10))
    pages = {"https://acme.com": links + '<a href="mailto:info@other1.com">x</a><a href="mailto:office@other2.com">y</a>'}
    scout = site_scout(pages)
    await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing")
    assert len(scout.fetched) <= scout_mod.MAX_SITE_FETCHES


@pytest.mark.parametrize("page", [
    "<html><title>Contact</title><body><form></form><p>This site is protected by reCAPTCHA and the Google Privacy Policy.</p></body></html>",
    "<html><title>Our Policies</title><body>Unauthorized access denied to staff area.</body></html>",
])
def test_ordinary_page_text_is_not_a_challenge(page):
    assert not Scout._looks_blocked(page)
