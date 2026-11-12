"""NO_SCHEMA_MARKUP recognises schema.org in all three formats.

The signal only looked for JSON-LD, so a site that marks itself up with
microdata (itemtype="https://schema.org/...") or RDFa (vocab="https://schema.org/")
was reported as having no structured data, and an email could tell its owner to
add what they already have.
"""

import pytest

from sellbird.collectors.profile import has_structured_data

JSON_LD = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Bakery"}</script>'
MICRODATA = '<div itemscope itemtype="https://schema.org/LocalBusiness"><span itemprop="name">Acme</span></div>'
MICRODATA_HTTP = "<div itemscope itemtype='http://schema.org/Dentist'></div>"
MICRODATA_WWW = '<div itemscope itemtype="https://www.schema.org/Organization"></div>'
RDFA = '<body vocab="https://schema.org/" typeof="Restaurant"><p property="name">Acme</p></body>'


@pytest.mark.parametrize("html", [JSON_LD, MICRODATA, MICRODATA_HTTP, MICRODATA_WWW, RDFA])
def test_each_format_counts(html):
    assert has_structured_data(f"<html><body>{html}</body></html>") is True


@pytest.mark.parametrize("html", [
    "<html><body><h1>Acme Bakery</h1></body></html>",
    "",
    # JSON-LD tag without a type is not usable markup, as before.
    '<script type="application/ld+json">{}</script>',
    # A link to schema.org in prose is not markup.
    '<p>See <a href="https://schema.org/Bakery">schema.org</a> for details.</p>',
    # itemtype for another vocabulary.
    '<div itemscope itemtype="https://example.test/Thing"></div>',
])
def test_no_markup_is_no_markup(html):
    assert has_structured_data(html) is False
