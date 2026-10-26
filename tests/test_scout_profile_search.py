"""LinkedIn profile-search parsing: company extraction and junk-name rejection.

All fixtures are fictional (acme.com-style placeholders, per CONTRIBUTING.md):
invented people with generic invented names, in the snippet shapes LinkedIn
actually returns.
"""

from harvey.agents.scout import Scout


def _scout():
    # The helpers only read class constants, so skip the heavy constructor.
    return Scout.__new__(Scout)


def test_company_from_dash_title():
    s = _scout()
    snippet = "Jane Doe - Founder - Acme Labs | LinkedIn\nFounder at Acme Labs."
    assert s._extract_linkedin_company(snippet) == "Acme Labs"


def test_company_from_at_title():
    s = _scout()
    assert s._extract_linkedin_company(
        "John Doe - Lab In-charge at Acme University | LinkedIn") == "Acme University"


def test_company_from_experience_field():
    s = _scout()
    snippet = ("Jane Doe\nExperience: Acme Infosystems · "
               "Education: Acme Institute · Location: Example City")
    assert s._extract_linkedin_company(snippet) == "Acme Infosystems"


def test_no_company_or_non_company_is_rejected():
    s = _scout()
    assert s._extract_linkedin_company("") == ""
    assert s._extract_linkedin_company("John Doe | LinkedIn") == ""
    assert s._extract_linkedin_company("Jane Doe - Designer - Freelance | LinkedIn") == ""


def test_title_words_are_not_names():
    s = _scout()
    assert s._looks_like_title_words({"first_name": "Startup", "last_name": "Founder"})
    assert s._looks_like_title_words({"first_name": "Senior", "last_name": "Software"})
    assert not s._looks_like_title_words({"first_name": "Jane", "last_name": "Doe"})
