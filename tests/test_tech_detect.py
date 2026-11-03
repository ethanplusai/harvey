"""The Scout's tech detector shares the AI-builder fingerprints with profiling."""

from harvey.integrations.tech_detect import detect_tech


def test_scout_detects_a_lovable_app_on_supabase():
    html = """<html><head>
      <meta name="author" content="Lovable" />
      <link rel="preconnect" href="https://abcdefghijklmnopqrst.supabase.co">
    </head><body><div id="root"></div></body></html>"""
    assert detect_tech(html) == ["Lovable", "Supabase"]


def test_scout_ignores_a_page_that_only_mentions_lovable():
    assert detect_tech("<p>Built something in Lovable? We can help.</p>") == []
