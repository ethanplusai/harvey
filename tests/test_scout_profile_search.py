"""LinkedIn profile-search parsing: company extraction and junk-name rejection."""

from harvey.agents.scout import Scout


def _scout():
    # The helpers only read class constants, so skip the heavy constructor.
    return Scout.__new__(Scout)


def test_company_from_dash_title():
    s = _scout()
    snippet = "Ilaya Bharathi - Founder - Kreate Labs | LinkedIn\nFounder at Kreate Labs."
    assert s._extract_linkedin_company(snippet) == "Kreate Labs"


def test_company_from_at_title():
    s = _scout()
    assert s._extract_linkedin_company("Riya Shah - Lab In-charge at JECRC University | LinkedIn") == "JECRC University"


def test_company_from_experience_field():
    s = _scout()
    snippet = "Vijayendra Singh\nExperience: Navyug Infosolutions · Education: MNIT · Location: Jaipur"
    assert s._extract_linkedin_company(snippet) == "Navyug Infosolutions"


def test_no_company_or_non_company_is_rejected():
    s = _scout()
    assert s._extract_linkedin_company("") == ""
    assert s._extract_linkedin_company("Shaju P G | LinkedIn") == ""
    assert s._extract_linkedin_company("Aman Jain - Designer - Freelance | LinkedIn") == ""


def test_title_words_are_not_names():
    s = _scout()
    assert s._looks_like_title_words({"first_name": "Startup", "last_name": "Founder"})
    assert s._looks_like_title_words({"first_name": "Senior", "last_name": "Software"})
    assert not s._looks_like_title_words({"first_name": "Surendra", "last_name": "Kankariya"})
