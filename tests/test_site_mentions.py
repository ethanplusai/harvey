"""SITE_MENTIONS: the user's icp.site_mentions phrases on a company's own pages."""

from types import SimpleNamespace

import pytest

from harvey.collectors.profile import ProfileCollector, mention_phrases, site_mentions
from harvey.signals import SIGNAL_CATALOG

PAD = "x" * 600


def test_phrases_match_whole_words_in_text_and_logo_alt_text():
    html = (f"<html><body>{PAD}<p>We run SIEM&nbsp;platforms for mid-market teams.</p>"
            "<img src='/l.png' alt='Splunk'><p>Splunk-certified engineers.</p></body></html>")
    assert site_mentions(html, ["Splunk", "SIEM platforms", "SOC"]) == ["Splunk", "SIEM platforms"]


def test_a_phrase_inside_another_word_does_not_match():
    assert site_mentions("<p>Splunker meetups and Datadogs</p>", ["Splunk", "Datadog"]) == []


def test_script_text_does_not_count():
    assert site_mentions("<script>var splunk = 1</script><p>Hello</p>", ["Splunk"]) == []


def test_phrases_come_from_the_config_trimmed_and_unique():
    icp = SimpleNamespace(site_mentions=[" Splunk ", "splunk", "", "managed  SOC"])
    assert mention_phrases(icp) == ["Splunk", "managed SOC"]
    assert mention_phrases(None) == []


def _collector(pages, mentions=("Splunk", "SIEM")):
    c = ProfileCollector(None, {"SITE_MENTIONS"}, mentions=list(mentions))
    requested = []

    async def fake_get(url):
        requested.append(url)
        return pages.get(url, "")

    c._get = fake_get
    c.requested = requested
    return c


HOME = f"<html><body>{PAD}<h1>Acme IT</h1><p>Managed services.</p></body></html>"


@pytest.mark.asyncio
async def test_reads_about_and_services_pages_until_every_phrase_is_found():
    pages = {
        "https://acme.example": HOME,
        "https://acme.example/about": f"<html>{PAD}<p>Our SIEM practice.</p></html>",
        "https://acme.example/services": f"<html>{PAD}<img alt='Splunk partner'></html>",
        "https://acme.example/partners": f"<html>{PAD}never reached</html>",
    }
    c = _collector(pages)
    [o] = await c.profile_company("c1", "acme.example")
    assert (o["signal_code"], o["value_num"], o["value_text"]) == ("SITE_MENTIONS", 2, "Splunk; SIEM")
    assert o["evidence_url"] == "https://acme.example/services"
    assert "https://acme.example/partners" not in c.requested


@pytest.mark.asyncio
async def test_a_homepage_with_every_phrase_fetches_nothing_more():
    home = HOME.replace("Managed services.", "Splunk and SIEM services.")
    c = _collector({"https://acme.example": home})
    [o] = await c.profile_company("c1", "acme.example")
    assert o["value_num"] == 2 and o["evidence_url"] == "https://acme.example"
    assert c.requested == ["https://acme.example"]


@pytest.mark.asyncio
async def test_silence_is_recorded_as_zero():
    c = _collector({"https://acme.example": HOME})
    [o] = await c.profile_company("c1", "acme.example")
    assert (o["value_num"], o["value_text"]) == (0, "")


@pytest.mark.asyncio
async def test_nothing_is_read_without_phrases_or_without_confirmation():
    assert await _collector({"https://acme.example": HOME}, mentions=()).profile_company(
        "c1", "acme.example") == []
    c = ProfileCollector(None, set(), mentions=["Splunk"])

    async def fake_get(url):
        return HOME.replace("Managed", "Splunk")
    c._get = fake_get
    assert await c.profile_company("c1", "acme.example") == []


def test_the_signal_is_in_the_catalog_as_a_free_profile_signal():
    sig = next(s for s in SIGNAL_CATALOG if s["code"] == "SITE_MENTIONS")
    assert sig["category"] == "profile" and sig["cost_note"].startswith("free")
