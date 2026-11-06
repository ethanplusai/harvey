"""Who decides at a small business, read off its own site.

Every fixture is fictional markup modelled on the shapes an audit of
small-business sites found Scout getting wrong (the wrong person, or
nobody): an owner in
a <span> with a <br>, a president named only in a sentence, founders in
schema.org, customers quoted in testimonials, practice-area links that look
like team pages, and addresses hidden from scrapers.
"""

import pytest

from sellbird.collectors import people
from sellbird.collectors.profile import ProfileCollector

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
    ("Dale Whitlock", ("Dale", "Whitlock")),
    ("Dr. Elliot T. Marwood", ("Elliot", "Marwood")),
    ("Renata D. Ashby", ("Renata", "Ashby")),
    ("Calvin P. Marsh, Esq.", ("Calvin", "Marsh")),
    ("John Smith DDS", ("John", "Smith")),
    ("Pieter Van de Wal", ("Pieter", "Van de Wal")),
    ("Maren Hollis Pryor", ("Maren", "Pryor")),
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

WHITLOCK = """
<p>Founded by Owen Whitlock in the 1970s, we've built a reputation. Today,
Whitlock&#8217;s remains a family-owned business with second-generation owner
Dale Whitlock at the helm.</p>
<div class="uagb-modal"><span class="uagb-modal-text">Dale Whitlock<br>Owner and CEO</span>
<div class="popup"><h3 class="uagb-ifb-title">Dale Whitlock</h3><p class="uagb-ifb-desc">I am the
second generation at Whitlock Heating &amp; Cooling. Growing up on job sites with my father.</p></div></div>
<span class="uagb-modal-text">Lena Park<br>Service Manager</span>
"""


def test_a_name_and_title_split_by_a_line_break_is_a_person():
    found = detect_people(WHITLOCK, domain="whitlockheating.test")
    assert ("Dale", "Whitlock") in names(found)
    dale = next(p for p in found if p["first_name"] == "Dale")
    assert dale["title"] == "Owner and CEO"


def test_a_bio_is_never_read_as_a_title():
    for p in detect_people(WHITLOCK, domain="whitlockheating.test"):
        assert "second generation" not in p["title"]


LANTERN = """
<h2>Team of Experts</h2><p>Owner, CEO</p>
<h3>Nate Calloway</h3><p>Owner, CEO</p><p>An absolute savage on the content, Nate Calloway started Lantern.</p>
<h3>Iris Vane</h3><p>Owner</p>
<h3>Rafe Sutton</h3><p>Creative Director</p>
"""


def test_a_heading_of_common_words_is_not_a_prospect():
    found = detect_people(LANTERN, domain="lanternagency.test")
    assert names(found) == [("Nate", "Calloway"), ("Iris", "Vane"), ("Rafe", "Sutton")]


TESTIMONIALS = """
<h2>What customers say</h2>
<p>We're massive users of Countwise here at Bluefinch Labs.</p>
<p>Oren Malik</p><p>Co-founder and CEO at Bluefinch Labs</p>
<p>Priya Holm</p><p>Founder at Quillnest</p>
<p>Tobias Wren, founder Signary and Bidloft</p>
<p>Hal Brandt , founder of Tallyway</p>
"""


def test_a_customer_quoted_on_the_page_is_not_the_owner():
    found = people.find_people([("https://tallyway.test", TESTIMONIALS)], "tallyway.test")
    assert names(found) == [("Hal", "Brandt")]


def test_a_tweet_quoted_on_the_homepage_is_not_staff():
    html = """<div class="p-4"><a class="flex" href="https://twitter.com/paulagrey/status/1234567890123456789">
      <img alt="Profile image"/><div class="font-bold">Paula Grey</div>
      <div class="text-xs">Product designer and founder</div></a>
      <div class="tweet-text">Just replaced my full network's analytics.</div></div>
      <div class="testimonial"><p>Dana Fox</p><p>Owner</p><p>Great service!</p></div>"""
    assert people.find_people([("https://countwise.test", html)], "countwise.test") == []


def test_a_company_after_a_title_is_not_a_name():
    html = "<p>“Fennick did a fantastic job.” RAY T. Managing Broker &amp; Founder, Birchwood Realty</p>"
    assert people.find_people([("https://fennickcreative.test", html)], "fennickcreative.test") == []


def test_a_tagline_is_not_a_title_and_a_later_card_wins():
    home = "<p>Meet Nate Calloway, proud advocate for the trades.</p>"
    about = "<h3>Nate Calloway</h3><p>Owner, CEO</p><h3>Iris Vane</h3><p>Owner</p>"
    found = people.find_people([("https://lanternagency.test", home),
                                ("https://lanternagency.test/about-us/", about)], "lanternagency.test")
    assert [(p["first_name"], p["title"]) for p in found] == [("Nate", "Owner, CEO"), ("Iris", "Owner")]


def test_meet_name_dash_title():
    html = "<h2>Meet Tess Marlowe - Founder &amp; Principal</h2><p>A true brand-builder.</p>"
    found = people.find_people([("https://marlowemarketingco.test/about", html)], "marlowemarketingco.test")
    assert names(found) == [("Tess", "Marlowe")] and found[0]["owner"]


def test_founder_named_by_first_name_is_matched_to_the_listed_person():
    html = """<h2>Renata D. Ashby</h2><p>Phone: (555) 555-0142</p>
      <p>Renata founded Ashby &amp; Kerr with longtime colleague Hugo Kerr in 2010.</p>
      <h2>Lise Moran</h2><p>Paralegal</p>"""
    found = people.find_people([("https://ashbykerr.test/team/", html)], "ashbykerr.test")
    assert names(found)[0] == ("Renata", "Ashby")
    assert found[0]["title"] == "Founder" and found[0]["owner"]


# ── owners named in prose ──

@pytest.mark.parametrize("sentence, domain, who, title", [
    ("Led today by President Ilan Brooks and partners Cody Fenn, Marc Lyle.",
     "ridgelineroofing.test", ("Ilan", "Brooks"), "President"),
    ("Today, it remains a family-owned business with second-generation owner Dale Whitlock at the helm.",
     "whitlockheating.test", ("Dale", "Whitlock"), "Owner"),
    ("Garrity Law Firm was founded by Colin Garrity, a family law attorney.",
     "garritylaw.test", ("Colin", "Garrity"), "Founder"),
    ("Founded by attorney Colin Garrity, the firm focuses on divorce.",
     "garritylaw.test", ("Colin", "Garrity"), "Founder"),
    ("Ines Varga started Countwise in March 2017, building it alone.",
     "countwise.test", ("Ines", "Varga"), "Founder"),
    ("We are proudly owned and operated by Gary Pruitt since 1998.",
     "pruittplumbing.test", ("Gary", "Pruitt"), "Owner"),
    ("Welcome to Hollis Family Dental, the office of Dr. Maren Hollis Pryor.",
     "hollisfamilydental.test", ("Maren", "Pryor"), "Owner"),
])
def test_owner_named_in_a_sentence(sentence, domain, who, title):
    found = people.people_from_prose(f"<p>{sentence}</p>", domain)
    assert names(found)[:1] == [who]
    assert found[0]["title"] == title and found[0]["owner"]


@pytest.mark.parametrize("sentence", [
    "Meet our founder and learn more about the company.",
    "Tobias Wren, founder Signary and Bidloft",
    "Being represented by an experienced attorney results in a better outcome.",
    "Our owner is on leave or has left the organization.",
])
def test_sentences_that_name_no_owner(sentence):
    assert people.people_from_prose(f"<p>{sentence}</p>", "countwise.test") == []


def test_first_person_founder_on_a_bio_page():
    html = ("<title>Attorney Neil Fairbanks | Fairbanks Law Firm, PLLC</title>"
            "<p>I am committed to protecting the rights of my neighbors. "
            "I founded the Fairbanks Law Firm, PLLC, in 2011.</p>")
    found = people.people_from_prose(html, "fairbankslaw.test")
    assert names(found) == [("Neil", "Fairbanks")] and found[0]["title"] == "Founder"


# ── schema.org ──

def test_schema_founders_are_owners():
    html = """<script type="application/ld+json">{"@context": "https://schema.org",
      "@type": "Dentist", "name": "Marwood Dental",
      "founder": ["Dr. Elliot Marwood", "Dr. Fiona Marwood"]}</script>"""
    found = people.people_from_schema(html)
    assert names(found) == [("Elliot", "Marwood"), ("Fiona", "Marwood")]
    assert all(p["owner"] and p["title"] == "Founder" for p in found)


def test_schema_employee_keeps_its_job_title_and_a_blog_author_is_ignored():
    html = """<script type="application/ld+json">{"@graph": [
      {"@type": "Organization", "@id": "#org", "name": "Acme Roofing",
       "employee": [{"@type": "Person", "name": "Dana Cole", "jobTitle": "General Manager"}],
       "founder": {"@id": "#kim"}},
      {"@type": "Person", "@id": "#kim", "name": "Kim Ortiz"},
      {"@type": "Article", "author": {"@type": "Person", "name": "Petra Lund"}},
      {"@type": "Person", "name": "Prairie Heritage Roofing"}
    ]}</script>"""
    found = people.people_from_schema(html)
    assert names(found) == [("Kim", "Ortiz"), ("Dana", "Cole")]
    assert [p["title"] for p in found] == ["Founder", "General Manager"]


def test_surname_on_the_door_never_makes_someone_the_owner():
    # At Smith & Sons, Linda Smith the office manager and Dr. Amy Smith the
    # associate dentist are family or staff, not owners.
    pages = [("https://smithandsonsroofing.test/team",
              "<h3>Linda Smith</h3><p>Office Manager</p>"
              "<h3>Dr. Amy Smith</h3><p>Associate Dentist</p>"
              "<h3>Maren Hollis Pryor</h3><p>DDS</p>")]
    found = people.find_people(pages, "smithandsonsroofing.test", "Smith & Sons Roofing")
    assert [p for p in found if p["owner"]] == []


def test_surname_on_the_door_breaks_a_tie_between_owners():
    pages = [("https://smithandsonsroofing.test/team",
              "<h3>Al Price</h3><p>Owner</p><h3>Bo Smith</h3><p>Owner</p>")]
    found = people.find_people(pages, "smithandsonsroofing.test", "Smith & Sons Roofing")
    assert names(found)[0] == ("Bo", "Smith")


SMITH = "smithandsonsroofing.test"


@pytest.mark.parametrize("html", [
    "<p>Smith &amp; Sons was founded in 1952 by the late Harold Smith.</p><p>Today it is run by his grandson.</p>",
    "<p>Founded by Harold Smith in 1952. Harold passed away in 1998.</p>",
    "<p>Our founder, Harold Smith, retired in 2010.</p>",
    "<p>\"Best roofer in town.\"</p><p>Rick Davis, Owner, Davis Bakery</p>",
    "<div class='slide'><p>“Fantastic work.”</p><p>Rick Davis</p><p>Owner</p></div>",
    "<div class='gr'><span>Karen White</span><span>Local Guide</span><p>Great owner!</p></div>",
    "<p>We are proud partners of KTR. Learn more from KTR President Vic Hale.</p>",
    "<p>We worked with Acme CEO Sarah Connor on the new HQ.</p>",
    "<p>President Alma Reyes visited our town.</p>",
    "<p>Principal Mary Alcott of Maplewood Elementary thanks us.</p>",
    "<p>Our youth league is led by Coach Gus Hart.</p>",
    "<p>Tom Reeve, President of the Ridgeport Roofing Association</p>",
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
    html = ("<p>“Great job.”</p><p>Rick Davis</p><p>Owner</p>"
            "<p>Learn more from KTR President Vic Hale.</p>"
            "<h3>Ilan Brooks</h3><p>President</p>")
    found = people.find_people([(f"https://{SMITH}/about", html)], SMITH)
    assert names(found)[0] == ("Ilan", "Brooks")


def test_owner_from_the_office_of_dr():
    html = "<p>Welcome to Hollis Family Dental, the office of Dr. Maren Hollis Pryor.</p>"
    found = people.find_people([("https://hollisfamilydental.test", html)], "hollisfamilydental.test")
    assert names(found) == [("Maren", "Pryor")] and found[0]["owner"]


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
    html = """<a href="/adoption-lawyer-riverton/">Adoption</a>
      <a href="/child-custody-lawyer-riverton/">Child Custody</a>
      <a href="/about-us/">About Us</a><a href="/our-attorneys/">Our Attorneys</a>"""
    assert people.menu_links(html, "https://garritylaw.test/") == [
        "https://garritylaw.test/our-attorneys/", "https://garritylaw.test/about-us/"]


def test_menu_links_respect_the_limit():
    many = "".join(f'<a href="/team/person-{i}/">Person {i}</a>' for i in range(20))
    assert len(people.menu_links(many, "https://acme.com", limit=5)) == 5


# ── addresses on a page ──

def _cf(address, key=0x42):
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in address)


def test_obfuscated_addresses_are_decoded():
    html = f"""
      <a class="tmf-email" href="#" data-front="rashby" data-back="ashbykerr.test"></a>
      <a href="/cdn-cgi/l/email-protection#{_cf('gil@harrowroofing.test')}">email</a>
      <span class="__cf_email__" data-cfemail="{_cf('info@harrowroofing.test')}">[email&#160;protected]</span>
      <a href="mailto:%20Tess@MarloweMarketingCo.test?subject=Hi">Tess</a>
      <p>Write to office [at] acme [dot] com or jane(at)acme.com</p>
      <p>you@example.com user@domain.com logo@2x.png</p>
    """
    assert set(people.emails_on_page(html)) == {
        "rashby@ashbykerr.test", "gil@harrowroofing.test", "info@harrowroofing.test",
        "tess@marlowemarketingco.test", "office@acme.com", "jane@acme.com",
    }


def test_schema_org_email_is_read():
    html = ('<script type="application/ld+json">{"@type": "RoofingContractor", '
            '"email": "admin@phroofing.test"}</script>')
    assert people.emails_on_page(html) == ["admin@phroofing.test"]


@pytest.mark.parametrize("html", [
    '<div data-name="Gus" data-website="acme.com"></div>',
    '<span data-u="tracking" data-d="googletagmanager.com"></span>',
    '<img data-src="a.png" data-user="x" data-domain="y.com">',
    "<p>Meet us at our office at Main dot Street</p>",
    "<p>Call Lou at acme dot com for details</p>",
    "<p>Reach Sam at 555 dot 1234</p>",
    "<p>Write to sales [at] acme [dot] notatld</p>",
])
def test_no_address_is_invented(html):
    assert people.emails_on_page(html) == []


def test_a_mailto_element_with_user_and_domain_attributes_is_decoded():
    html = '<a href="#" class="email-link" data-user="lou" data-domain="acme.com">Email</a>'
    assert people.emails_on_page(html) == ["lou@acme.com"]


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
    html = "<p>x</p>" * 30000 + '<a href="mailto:info@summitcrestroofingandsiding.test">Email</a>'
    assert people.emails_on_page(html) == ["info@summitcrestroofingandsiding.test"]
