"""Who decides at a small business, read off its own site.

Every fixture is a trimmed copy of markup from a real small-business site
where an audit found Scout picking the wrong person, or nobody: an owner in
a <span> with a <br>, a president named only in a sentence, founders in
schema.org, customers quoted in testimonials, practice-area links that look
like team pages, and addresses hidden from scrapers.
"""

import pytest

from harvey.collectors import people
from harvey.collectors.profile import ProfileCollector

detect_people = ProfileCollector(state=None).detect_people


def names(found):
    return [(p["first_name"], p["last_name"]) for p in found]


# ── who is a person ──

@pytest.mark.parametrize("text", [
    "Team of Experts", "Our People", "Meet The Team", "Customer Care",
    "Free Estimate", "Read More", "Family Owned", "Quality Roofing",
])
def test_common_words_are_not_a_person(text):
    assert not people.is_person_name(text)


@pytest.mark.parametrize("text, expected", [
    ("Jason Martens", ("Jason", "Martens")),
    ("Dr. Gerald T. Shinkawa", ("Gerald", "Shinkawa")),
    ("Michelle D. Connell", ("Michelle", "Connell")),
    ("Bryan P. Webber, Esq.", ("Bryan", "Webber")),
    ("John Smith DDS", ("John", "Smith")),
    ("Leendert Van de Rydt", ("Leendert", "Van de Rydt")),
    ("Christie Rosenberry Damm", ("Christie", "Damm")),
])
def test_names_are_split_without_honorifics_initials_or_credentials(text, expected):
    assert people.split_person_name(text) == expected


# ── titles ──

@pytest.mark.parametrize("title, targets, expected", [
    ("Vice President of Sales", ["President"], False),
    ("Vice President of Sales", ["Vice President"], True),
    ("VP, Operations", ["President"], False),
    ("Executive Assistant to the CEO", ["CEO"], False),
    ("Partnerships Manager", ["Partner"], False),
    ("Owner/Operator", ["Owner"], True),
    ("Proprietor", ["Owner"], True),
    ("Principal", ["Owner"], True),
    ("Managing Partner", ["Owner"], True),
    ("Co-founder", ["Founder"], True),
    ("Co-Owner", ["Owner"], True),
    ("Founder & CEO", ["President"], True),
    ("Owner, CEO", ["Owner"], True),
    ("Office Manager", ["Owner", "President"], False),
    ("General Manager", ["General Manager"], True),
    ("Senior Operations Manager", ["Operations Manager"], True),
])
def test_title_matching_is_word_based_and_knows_owner_synonyms(title, targets, expected):
    assert people.title_matches(title, targets) is expected


def test_a_practice_owner_satisfies_an_owner_icp_without_the_word():
    # "DDS" alone is not an owner title; the dentist whose name is on the door is.
    assert not people.title_matches("DDS", ["Owner"])
    assert people.title_matches("DDS", ["Owner"], owner=True)
    # ...but owning the business never turns them into a marketing director.
    assert not people.title_matches("DDS", ["Marketing Director"], owner=True)


# ── people on a page ──

MARTENS = """
<p>Founded by Dean Martens in the 1970s, we've built a reputation. Today,
Marten&#8217;s remains a family-owned business with second-generation owner
Jason Martens at the helm.</p>
<div class="uagb-modal"><span class="uagb-modal-text">Jason Martens<br>Owner and CEO</span>
<div class="popup"><h3 class="uagb-ifb-title">Jason Martens</h3><p class="uagb-ifb-desc">I am the
second generation at Martens Heating &amp; Cooling. Growing up on job sites with my father.</p></div></div>
<span class="uagb-modal-text">Wes Henry<br>Service Manager</span>
"""


def test_a_name_and_title_split_by_a_line_break_is_a_person():
    found = detect_people(MARTENS, domain="martensheating.com")
    assert ("Jason", "Martens") in names(found)
    jason = next(p for p in found if p["first_name"] == "Jason")
    assert jason["title"] == "Owner and CEO"


def test_a_bio_is_never_read_as_a_title():
    for p in detect_people(MARTENS, domain="martensheating.com"):
        assert "second generation" not in p["title"]


HOOK = """
<h2>Team of Experts</h2><p>Owner, CEO</p>
<h3>Tim Brown</h3><p>Owner, CEO</p><p>An absolute savage on the content, Tim Brown started Hook.</p>
<h3>Bea Bonte</h3><p>Owner</p>
<h3>Matt Anderson</h3><p>Creative Director</p>
"""


def test_a_heading_of_common_words_is_not_a_prospect():
    found = detect_people(HOOK, domain="hookagency.com")
    assert names(found) == [("Tim", "Brown"), ("Bea", "Bonte"), ("Matt", "Anderson")]


TESTIMONIALS = """
<h2>What customers say</h2>
<p>We're massive users of Plausible here at Hugging Face.</p>
<p>Clem Delangue</p><p>Co-founder and CEO at Hugging Face</p>
<p>Laura Roeder</p><p>Founder at Paperbell</p>
<p>Ruben Gamez, founder SignWell and Bidsketch</p>
<p>Jack Ellis , founder of Fathom</p>
"""


def test_a_customer_quoted_on_the_page_is_not_the_owner():
    found = people.find_people([("https://usefathom.com", TESTIMONIALS)], "usefathom.com")
    assert names(found) == [("Jack", "Ellis")]


def test_a_tweet_quoted_on_the_homepage_is_not_staff():
    html = """<div class="p-4"><a class="flex" href="https://twitter.com/robhope/status/1351465826109558784">
      <img alt="Profile image"/><div class="font-bold">Rob Hope</div>
      <div class="text-xs">Product designer and founder</div></a>
      <div class="tweet-text">Just replaced my full network's analytics.</div></div>
      <div class="testimonial"><p>Dana Fox</p><p>Owner</p><p>Great service!</p></div>"""
    assert people.find_people([("https://plausible.io", html)], "plausible.io") == []


def test_a_company_after_a_title_is_not_a_name():
    html = "<p>“Klare did a fantastic job.” JEFF P. Managing Broker &amp; Founder, Hatch Realty</p>"
    assert people.find_people([("https://klarecreative.com", html)], "klarecreative.com") == []


def test_a_tagline_is_not_a_title_and_a_later_card_wins():
    home = "<p>Meet Tim Brown, proud advocate for the Home Service Industry.</p>"
    about = "<h3>Tim Brown</h3><p>Owner, CEO</p><h3>Bea Bonte</h3><p>Owner</p>"
    found = people.find_people([("https://hookagency.com", home),
                                ("https://hookagency.com/about-us/", about)], "hookagency.com")
    assert [(p["first_name"], p["title"]) for p in found] == [("Tim", "Owner, CEO"), ("Bea", "Owner")]


def test_meet_name_dash_title():
    html = "<h2>Meet Beth Hardy - Founder &amp; Principal</h2><p>A true brand-builder.</p>"
    found = people.find_people([("https://hardymarketingco.com/about", html)], "hardymarketingco.com")
    assert names(found) == [("Beth", "Hardy")] and found[0]["owner"]


def test_founder_named_by_first_name_is_matched_to_the_listed_person():
    html = """<h2>Michelle D. Connell</h2><p>Phone: (919) 754-4901</p>
      <p>Michelle founded Connell &amp; Gelb with longtime colleague Wes Gelb in 2010.</p>
      <h2>Kate Barefoot</h2><p>Paralegal</p>"""
    found = people.find_people([("https://connellgelb.com/team/", html)], "connellgelb.com")
    assert names(found)[0] == ("Michelle", "Connell")
    assert found[0]["title"] == "Founder" and found[0]["owner"]


# ── owners named in prose ──

@pytest.mark.parametrize("sentence, domain, who, title", [
    ("Led today by President Rami Zarifa and partners David Heidt, Scott Kawulok.",
     "bmroofing.com", ("Rami", "Zarifa"), "President"),
    ("Today, it remains a family-owned business with second-generation owner Jason Martens at the helm.",
     "martensheating.com", ("Jason", "Martens"), "Owner"),
    ("Feitelson Law Firm was founded by Jeremy Feitelson, an Iowa family law attorney.",
     "feitelsonlaw.com", ("Jeremy", "Feitelson"), "Founder"),
    ("Founded by attorney Jeremy Feitelson, the firm focuses on divorce.",
     "feitelsonlaw.com", ("Jeremy", "Feitelson"), "Founder"),
    ("Uku Taht started Plausible in December 2018, building it alone.",
     "plausible.io", ("Uku", "Taht"), "Founder"),
    ("We are proudly owned and operated by Mike Jones since 1998.",
     "jonesplumbing.com", ("Mike", "Jones"), "Owner"),
    ("Welcome to Rosenberry Family Dental, the office of Dr. Christie Rosenberry Damm.",
     "rosenberryfamilydental.com", ("Christie", "Damm"), "Owner"),
])
def test_owner_named_in_a_sentence(sentence, domain, who, title):
    found = people.people_from_prose(f"<p>{sentence}</p>", domain)
    assert names(found)[:1] == [who]
    assert found[0]["title"] == title and found[0]["owner"]


@pytest.mark.parametrize("sentence", [
    "Meet our founder and learn more about the company.",
    "Ruben Gamez, founder SignWell and Bidsketch",
    "Being represented by an experienced attorney results in a better outcome.",
    "Our owner is on leave or has left the organization.",
])
def test_sentences_that_name_no_owner(sentence):
    assert people.people_from_prose(f"<p>{sentence}</p>", "plausible.io") == []


def test_first_person_founder_on_a_bio_page():
    html = ("<title>Attorney Stuart Higgins | Higgins Law Firm, PLLC</title>"
            "<p>I am committed to protecting the rights of my fellow Iowans. "
            "I founded the Higgins Law Firm, PLLC, in 2011.</p>")
    found = people.people_from_prose(html, "higginslawiowa.com")
    assert names(found) == [("Stuart", "Higgins")] and found[0]["title"] == "Founder"


# ── schema.org ──

def test_schema_founders_are_owners():
    html = """<script type="application/ld+json">{"@context": "https://schema.org",
      "@type": "Dentist", "name": "Shinkawa Dental",
      "founder": ["Dr. Gerald Shinkawa", "Dr. Nicole Shinkawa"]}</script>"""
    found = people.people_from_schema(html)
    assert names(found) == [("Gerald", "Shinkawa"), ("Nicole", "Shinkawa")]
    assert all(p["owner"] and p["title"] == "Founder" for p in found)


def test_schema_employee_keeps_its_job_title_and_a_blog_author_is_ignored():
    html = """<script type="application/ld+json">{"@graph": [
      {"@type": "Organization", "@id": "#org", "name": "Acme Roofing",
       "employee": [{"@type": "Person", "name": "Dana Cole", "jobTitle": "General Manager"}],
       "founder": {"@id": "#kim"}},
      {"@type": "Person", "@id": "#kim", "name": "Kim Ortiz"},
      {"@type": "Article", "author": {"@type": "Person", "name": "Denisa Lalikova"}},
      {"@type": "Person", "name": "Texas Traditions Roofing"}
    ]}</script>"""
    found = people.people_from_schema(html)
    assert names(found) == [("Kim", "Ortiz"), ("Dana", "Cole")]
    assert [p["title"] for p in found] == ["Founder", "General Manager"]


def test_surname_on_the_door_never_makes_someone_the_owner():
    # At Smith & Sons, Linda Smith the office manager and Dr. Amy Smith the
    # associate dentist are family or staff, not owners.
    pages = [("https://smithandsonsroofing.com/team",
              "<h3>Linda Smith</h3><p>Office Manager</p>"
              "<h3>Dr. Amy Smith</h3><p>Associate Dentist</p>"
              "<h3>Christie Rosenberry Damm</h3><p>DDS</p>")]
    found = people.find_people(pages, "smithandsonsroofing.com", "Smith & Sons Roofing")
    assert [p for p in found if p["owner"]] == []


def test_surname_on_the_door_breaks_a_tie_between_owners():
    pages = [("https://smithandsonsroofing.com/team",
              "<h3>Al Brown</h3><p>Owner</p><h3>Bo Smith</h3><p>Owner</p>")]
    found = people.find_people(pages, "smithandsonsroofing.com", "Smith & Sons Roofing")
    assert names(found)[0] == ("Bo", "Smith")


SMITH = "smithandsonsroofing.com"


@pytest.mark.parametrize("html", [
    "<p>Smith &amp; Sons was founded in 1952 by the late Harold Smith.</p><p>Today it is run by his grandson.</p>",
    "<p>Founded by Harold Smith in 1952. Harold passed away in 1998.</p>",
    "<p>Our founder, Harold Smith, retired in 2010.</p>",
    "<p>\"Best roofer in town.\"</p><p>Mike Davis, Owner, Davis Bakery</p>",
    "<div class='slide'><p>“Fantastic work.”</p><p>Mike Davis</p><p>Owner</p></div>",
    "<div class='gr'><span>Karen White</span><span>Local Guide</span><p>Great owner!</p></div>",
    "<p>We are proud partners of GAF. Learn more from GAF President Jim Schnepper.</p>",
    "<p>We worked with Acme CEO Sarah Connor on the new HQ.</p>",
    "<p>President Joe Biden visited our town.</p>",
    "<p>Principal Mary Jones of Lincoln Elementary thanks us.</p>",
    "<p>Our youth league is led by Coach Tim Hart.</p>",
    "<p>Tom Brown, President of the Denver Roofing Association</p>",
    "<h3>Harold Smith</h3><p>Former Owner</p>",
    "<h3>Harold Smith</h3><p>Founder (1952-2001)</p>",
    "<h3>Harold Smith</h3><p>Retired President</p>",
    "<h3>Harold Smith</h3><p>Past President</p>",
    "<h3>In Memory of Harold Smith</h3><p>Founder</p>",
])
def test_not_a_current_owner_of_this_business(html):
    found = people.find_people([(f"https://{SMITH}/about", f"<html><body>{html}</body></html>")],
                               SMITH, "Smith & Sons Roofing")
    assert [(p["first_name"], p["last_name"], p["title"]) for p in found if p["owner"]] == []


@pytest.mark.parametrize("title", ["Former Owner", "Founder (1952-2001)", "Retired President",
                                   "Past President", "President Emeritus", "Late Founder"])
def test_former_owners_are_not_owner_titles(title):
    assert not people.is_owner_title(title)
    assert not people.title_matches(title, ["Owner", "President", "Founder"])


def test_the_real_president_outranks_false_positives():
    html = ("<p>“Great job.”</p><p>Mike Davis</p><p>Owner</p>"
            "<p>Learn more from GAF President Jim Schnepper.</p>"
            "<h3>Rami Zarifa</h3><p>President</p>")
    found = people.find_people([(f"https://{SMITH}/about", html)], SMITH)
    assert names(found)[0] == ("Rami", "Zarifa")


def test_owner_from_the_office_of_dr():
    html = "<p>Welcome to Rosenberry Family Dental, the office of Dr. Christie Rosenberry Damm.</p>"
    found = people.find_people([("https://rosenberryfamilydental.com", html)], "rosenberryfamilydental.com")
    assert names(found) == [("Christie", "Damm")] and found[0]["owner"]


def test_owners_rank_ahead_of_staff():
    pages = [("https://acme.com/team",
              "<h3>Sam Lee</h3><p>Office Manager</p><h3>Pat Kim</h3><p>Owner</p>")]
    assert names(people.find_people(pages, "acme.com"))[0] == ("Pat", "Kim")


# ── which pages to read ──

HOME = """
<nav>
  <a href="/">Home</a> <a href="#main">Skip</a> <a href="tel:5551234">Call</a>
  <a href="/services/roof-repair/">Roof Repair</a>
  <a href="/about-us/">About</a>
  <a href="https://www.acme.com/meet-the-dentists.html">Meet the Dentists</a>
  <a href="/contact/">Contact</a>
  <a href="https://facebook.com/acme">Facebook</a>
  <a href="/brochure.pdf">Our team (PDF)</a>
  <a href="/blog/2024/05/our-team-at-the-expo/">Our team at the expo</a>
</nav>"""


def test_menu_links_are_scored_and_stay_on_the_site():
    links = people.menu_links(HOME, "https://www.acme.com/", limit=5)
    assert links == [
        "https://www.acme.com/meet-the-dentists.html",
        "https://www.acme.com/about-us/",
        "https://www.acme.com/contact/",
    ]


def test_practice_area_pages_are_not_people_pages():
    html = """<a href="/adoption-lawyer-des-moines/">Adoption</a>
      <a href="/child-custody-lawyer-des-moines/">Child Custody</a>
      <a href="/about-us/">About Us</a><a href="/our-attorneys/">Our Attorneys</a>"""
    assert people.menu_links(html, "https://feitelsonlaw.com/") == [
        "https://feitelsonlaw.com/our-attorneys/", "https://feitelsonlaw.com/about-us/"]


def test_menu_links_respect_the_limit():
    many = "".join(f'<a href="/team/person-{i}/">Person {i}</a>' for i in range(20))
    assert len(people.menu_links(many, "https://acme.com", limit=5)) == 5


# ── addresses on a page ──

def _cf(address, key=0x42):
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in address)


def test_obfuscated_addresses_are_decoded():
    html = f"""
      <a class="tmf-email" href="#" data-front="mconnell" data-back="connellgelb.com"></a>
      <a href="/cdn-cgi/l/email-protection#{_cf('ryan@westfallroofing.com')}">email</a>
      <span class="__cf_email__" data-cfemail="{_cf('info@westfallroofing.com')}">[email&#160;protected]</span>
      <a href="mailto:%20Beth@HardyMarketingCo.com?subject=Hi">Beth</a>
      <p>Write to office [at] acme [dot] com or jane(at)acme.com</p>
      <p>you@example.com user@domain.com logo@2x.png</p>
    """
    assert set(people.emails_on_page(html)) == {
        "mconnell@connellgelb.com", "ryan@westfallroofing.com", "info@westfallroofing.com",
        "beth@hardymarketingco.com", "office@acme.com", "jane@acme.com",
    }


def test_schema_org_email_is_read():
    html = ('<script type="application/ld+json">{"@type": "RoofingContractor", '
            '"email": "admin@txtroofing.com"}</script>')
    assert people.emails_on_page(html) == ["admin@txtroofing.com"]


@pytest.mark.parametrize("html", [
    '<div data-name="Mike" data-website="acme.com"></div>',
    '<span data-u="tracking" data-d="googletagmanager.com"></span>',
    '<img data-src="a.png" data-user="x" data-domain="y.com">',
    "<p>Meet us at our office at Main dot Street</p>",
    "<p>Call Jim at acme dot com for details</p>",
    "<p>Reach Sam at 555 dot 1234</p>",
    "<p>Write to sales [at] acme [dot] notatld</p>",
])
def test_no_address_is_invented(html):
    assert people.emails_on_page(html) == []


def test_a_mailto_element_with_user_and_domain_attributes_is_decoded():
    html = '<a href="#" class="email-link" data-user="jim" data-domain="acme.com">Email</a>'
    assert people.emails_on_page(html) == ["jim@acme.com"]


ADVERSARIAL = {
    "hex": lambda: "<div data-x='" + "ab12" * 375_000 + "'></div>",     # 1.5MB of hex
    "dotted": lambda: "<p>" + ".".join(["abc"] * 375_000) + "</p>",
    "long-run": lambda: "<p>" + "a" * 1_500_000 + "</p>",
    "at-words": lambda: "<p>" + "a- " * 500_000 + " at </p>",
}


@pytest.mark.parametrize("kind", sorted(ADVERSARIAL))
def test_address_scan_is_linear_on_adversarial_pages(kind):
    import time
    html = ADVERSARIAL[kind]()
    start = time.monotonic()
    people.emails_on_page(html)
    assert time.monotonic() - start < 1.0


def test_nested_testimonials_are_bounded():
    import time
    html = "<div class='testimonial'>" * 2000 + "x" + "</div>" * 2000
    start = time.monotonic()
    people.drop_quoted(html)
    people.find_people([("https://acme.com", html)], "acme.com")
    assert time.monotonic() - start < 1.0


def test_whole_page_is_scanned_not_just_the_top():
    html = "<p>x</p>" * 30000 + '<a href="mailto:info@integrityroofingandsiding.com">Email</a>'
    assert people.emails_on_page(html) == ["info@integrityroofingandsiding.com"]
