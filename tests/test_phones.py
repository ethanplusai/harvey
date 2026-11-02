"""Phone and WhatsApp numbers read from a business's own pages."""

from harvey.collectors import phones

PAGE = """
<html><head><script type="application/ld+json">
{"@type": "LocalBusiness", "name": "Example Co", "telephone": "+971 4 000 0000"}
</script></head><body>
<a href="tel:+97140000000">Call us</a>
<a href="https://wa.me/971500000001?text=hi">WhatsApp</a>
<p>Mobile: +971 (0)55 000 0002</p>
</body></html>
"""


def test_finds_phone_and_whatsapp_and_prefers_mobile():
    nums = phones.contact_numbers([("https://example.ae", PAGE)], "", "example.ae")
    assert nums["whatsapp"] == "971500000001"
    assert nums["phone"] == "971550000002"


def test_local_number_takes_country_from_domain_or_location():
    assert phones.international("050 000 0003", "", "example.ae") == "971500000003"
    assert phones.international("055 000 0004", "Riyadh, Saudi Arabia") == "966550000004"
    assert phones.international("050 000 0003") == ""


def test_api_whatsapp_link_and_pretty():
    page = '<a href="https://api.whatsapp.com/send?phone=97450000005">chat</a>'
    assert phones.numbers_on_page(page)[1] == ["+97450000005"]
    assert phones.pretty("971500000001") == "+971 50 000 0001"
    assert phones.is_mobile("971500000001") and not phones.is_mobile("97140000000")


def test_no_numbers():
    assert phones.contact_numbers([("https://example.com", "<p>hello</p>")]) == {"phone": "", "whatsapp": ""}


def test_review_offers_only_personal_or_mobile_numbers():
    from harvey.dashboard import _numbers
    office_only = _numbers({"company_phone": "+971 4 000 0000", "domain": "example.ae"})
    assert office_only["phone"] == "" and office_only["whatsapp"] == ""
    assert office_only["office_phone_display"] == "+971 40 000 000"

    mobile = _numbers({"company_phone": "+971 4 000 0000", "whatsapp": "+971500000001",
                       "domain": "example.ae", "linkedin_url": "https://www.linkedin.com/in/example"})
    assert mobile["phone"] == "971500000001" and mobile["whatsapp_from_site"]
    assert mobile["linkedin_url"] == "https://www.linkedin.com/in/example"

    own = _numbers({"person_phone": "050 000 0003", "company_phone": "+971500000001", "domain": "example.ae"})
    assert own["phone"] == "971500000003" and own["phone_is_personal"]
