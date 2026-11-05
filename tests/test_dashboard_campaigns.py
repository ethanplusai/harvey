"""The Campaigns tab: one row per campaign, found by search and filter."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parent.parent / "harvey" / "web" / "app.js").read_text(encoding="utf-8")


def _run(expression):
    """Evaluate an expression under node with app.js's real campaign helpers."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    parts = []
    for pattern in (r"^function escHtml\(.*?^}\n", r"^function campaignMatches\(.*?^}\n",
                    r"^function campaignSteps\(.*?^}\n", r"^function campaignRow\(.*?^}\n",
                    r"^function campaignPanel\(.*?^}\n"):
        match = re.search(pattern, SOURCE, re.S | re.M)
        assert match, pattern
        parts.append(match.group(0))
    stubs = ("const badge = s => s; const relTime = s => s; const campaignProgress = () => '';"
             "const jsq = s => s;")
    script = stubs + "\n".join(parts) + f"\nprocess.stdout.write(JSON.stringify({expression}));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8",
                         check=True, timeout=30).stdout
    return json.loads(out)


CAMPAIGN = {
    "id": "c1", "name": "Roofers", "status": "active", "prospect_ids": ["p1"],
    "sequence": [{"step": 1, "subject": "your yard", "body": "Hello"}],
    "recipients": [{"id": "p1", "email": "pat@x.test", "first_name": "Pat", "last_name": "Lee",
                    "company": "Acme", "status": "queued"}],
}


def _matches(query, statuses=None):
    return _run(f"campaignMatches({json.dumps(CAMPAIGN)}, {json.dumps(query)}, {json.dumps(statuses)})")


@pytest.mark.parametrize("query", ["", "roof", "YOUR YARD", "pat@x", "lee", "acme", "  acme  "])
def test_search_covers_the_name_people_and_subjects(query):
    assert _matches(query)


def test_search_misses_what_isnt_there():
    assert not _matches("plumber")


def test_a_filter_matches_any_person_in_the_campaign():
    assert _matches("", ["new", "queued"])
    assert not _matches("", ["replied"])
    assert not _matches("acme", ["replied"])


def test_the_row_lists_the_campaign_and_the_panel_holds_its_emails():
    row = _run(f"campaignRow({json.dumps(CAMPAIGN)}, 7)")
    panel = _run(f"campaignPanel({json.dumps(CAMPAIGN)}, 7)")
    assert "Roofers" in row and "openCampaign(7)" in row and "your yard" not in row
    assert "your yard" in panel and "fbCampaign(7)" in panel


def test_the_row_and_panel_escape_what_harvey_wrote():
    bad = json.dumps({**CAMPAIGN, "name": "<img src=x>", "id": 'a"b'})
    row = _run(f"campaignRow({bad}, 0)")
    panel = _run(f"campaignPanel({bad}, 0)")
    assert "<img" not in row and "<img" not in panel
    assert 'data-id="a&quot;b"' in row
