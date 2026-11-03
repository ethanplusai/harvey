"""The possessive merge: "{{company}}'s" for a name ending in s."""

from types import SimpleNamespace

from sellbird.agents.sender import Sender


def _render(text, company):
    s = Sender.__new__(Sender)
    prospect = SimpleNamespace(first_name="Dana", last_name="Lee", company=company,
                               title="", personalization_notes="",
                               is_company_inbox=False)
    return s._render(text, prospect)


# ── possessive merge ──

def test_possessive_for_company_ending_in_s():
    assert _render("in {{company}}'s voice", "Parkes Builders") == "in Parkes Builders' voice"


def test_possessive_for_other_companies_unchanged():
    assert _render("in {{company}}'s voice", "Acme Dental") == "in Acme Dental's voice"
