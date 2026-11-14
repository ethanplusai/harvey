"""The ICP filter: who is worth emailing, and who is not.

A search for "manufacturing company Pune" returns what Google ranks — and
Google ranks blog posts, directories, marketplaces and listicles alongside
real factories. A search for "plastic manufacturer" returns an ERP vendor's
blog about plastic manufacturing, and that vendor then enters the pipeline
as though it were a prospect.

These tests pin the three things the filter has to get right:

1. A page about companies is not a company.
2. An ERP vendor, a software house or a trading company is not a buyer.
3. A manufacturer that merely *uses* software, or happens to have "Tech"
   in its name, is still a buyer and must survive the filter.
"""

import pytest

from sellbird.icp_filter import (
    exclusion_reason,
    is_content_page,
    is_it_vendor,
    is_maharashtra,
    is_trading_or_retail,
    location_matches_priority,
    maharashtra_priority,
    place_key,
)


# ── 1. Content pages are not companies ─────────────────────────────────


@pytest.mark.parametrize("name,url", [
    ("Manufacturing Companies in Nagpur",
     "https://www.nagpuronline.in/guide/manufacturing-companies-in-nagpur"),
    ("ShipBob", "https://www.shipbob.com/blog/retail-distribution/"),
    ("FarEye", "https://fareye.com/resources/blogs/what-is-retail-distribution"),
    ("Top 50 Plastic Manufacturers in India",
     "https://example.com/blog/top-50-plastic-manufacturers"),
    ("IIA Industrial Directory", "https://example.com/directory/industrial"),
    ("MSME B2B Portal", "https://example.com/portal/msme"),
])
def test_a_page_about_companies_is_not_a_company(name, url):
    assert is_content_page(name, url)
    assert exclusion_reason({"name": name, "source_url": url})


def test_a_real_company_page_is_a_company():
    assert not is_content_page(
        "Acme Polymers", "https://acmepolymers.com/about")
    assert not is_content_page(
        "Acme Polymers", "https://acmepolymers.com/products/pipe-fittings")


# ── 2. Vendors are not buyers ──────────────────────────────────────────


@pytest.mark.parametrize("name,industry", [
    ("Delta Sales App", "Retail and Distribution"),
    ("Odoo", "ERP"),
    ("Tally", "Accounting Software"),
    ("Zoho", "Software"),
    ("Acme IT Services", "Information Technology"),
    ("Bright Web Development", "Web Development"),
    ("QuickCommerceMap", "Logistics"),
    ("MSME B2B Portal", "Manufacturing"),
])
def test_a_software_vendor_is_not_a_prospect(name, industry):
    assert is_it_vendor(name, industry)
    assert exclusion_reason({"name": name, "industry": industry})


def test_a_vendor_phrase_in_the_description_is_enough():
    assert is_it_vendor(
        "Bharat Solutions",
        "Manufacturing",
        "We are a software company offering ERP solutions for factories.")
    assert is_it_vendor(
        "Bharat Solutions",
        "Manufacturing",
        "Acme provides cloud services to mid-sized manufacturers.")


# ── 3. Manufacturers are not caught by the vendor filter ───────────────


@pytest.mark.parametrize("name,industry,description", [
    ("Hi-Tech Engineering", "Engineering and Fabrication", ""),
    ("Power Tech Products", "Electrical Equipment Manufacturing", ""),
    ("System Engineering Works", "engineering works", ""),
    ("Umiya Hydraulic System", "hydraulic equipment manufacturer", ""),
    ("Avcon System", "Engineering", ""),
    ("Acme Polymers", "polymer manufacturer",
     "We use ERP software for billing and Tally for accounting."),
    ("Acme Polymers", "polymer manufacturer",
     "Our shopfloor runs on Odoo and SAP Business One."),
])
def test_a_manufacturer_that_uses_software_is_still_a_prospect(name, industry, description):
    """The exact reason the vendor filter must be phrase-specific.

    A manufacturer that says "we use Odoo" is a buyer. A vendor that says
    "we are an Odoo company" is not. The difference is worth thousands of
    rupees per email, so the filter has to get it right.
    """
    assert not is_it_vendor(name, industry, description)


def test_a_manufacturer_with_tech_in_its_name_survives():
    assert not is_it_vendor("Hi-Tech Engineering", "Engineering and Fabrication")
    assert not exclusion_reason({
        "name": "Hi-Tech Engineering",
        "industry": "Engineering and Fabrication",
        "source_url": "https://hitechengineering.com/about",
    })


# ── 4. Trading, retail and distribution ────────────────────────────────


@pytest.mark.parametrize("name,industry", [
    ("Nehru Place Computer Market", "Retail and Distribution"),
    # The industry phrase on its own is the search term, not a
    # classification (see is_trading_or_retail): the name must say it.
    ("SAHI Retail", "Retail and Distribution"),
    ("Reliance Retail Ltd", "Retail and Distribution"),
    ("M Y Trading Co., Pune", "trading company"),
    ("Integral Trading & Logistics", "Trading"),
    ("Lazarus Trading Company", "trading company"),
])
def test_a_trading_house_is_not_a_prospect(name, industry):
    assert is_trading_or_retail(name, industry)
    assert exclusion_reason({"name": name, "industry": industry})


def test_a_manufacturer_that_exports_is_still_a_prospect():
    """Exporting is how a manufacturer sells. Only trading is out."""
    assert not is_trading_or_retail(
        "Hydraulic Power Pack Manufacturer & Exporter",
        "Hydraulics and Heavy Equipment Manufacturing")


# ── 5. Maharashtra ─────────────────────────────────────────────────────


@pytest.mark.parametrize("location", [
    "Pune", "Pune, India", "Nashik", "Mumbai", "Thane, India",
    "Chhatrapati Sambhajinagar", "Nagpur", "Kolhapur, India",
])
def test_maharashtra_places_are_recognised(location):
    assert is_maharashtra(location)
    assert maharashtra_priority(location) == 0


@pytest.mark.parametrize("location", [
    "Coimbatore", "Rajkot", "Surat", "Ludhiana", "", "Bengaluru",
])
def test_elsewhere_is_not_maharashtra(location):
    assert not is_maharashtra(location)
    assert maharashtra_priority(location) == 1


def test_a_place_matches_by_city_not_by_whole_string():
    assert place_key("Pune, India") == place_key("Pune") == "pune"
    assert location_matches_priority("Pune", ["Pune, India"])
    assert location_matches_priority("Pune, India", ["Pune"])
    assert not location_matches_priority("Surat", ["Pune, India"])


# ── 6. The reason a company is excluded ────────────────────────────────


def test_the_reason_names_the_problem():
    assert "blog" in exclusion_reason({
        "name": "X", "source_url": "https://x.com/blog/post"})
    assert "IT" in exclusion_reason({"name": "X", "industry": "Software"})
    assert "trading" in exclusion_reason({
        "name": "X Trading Co", "industry": "trading company"})


def test_a_good_manufacturer_has_no_reason():
    assert exclusion_reason({
        "name": "Acme Polymers",
        "industry": "polymer manufacturer",
        "description": "We make HDPE pipes for irrigation.",
        "source_url": "https://acmepolymers.com/",
    }) == ""
