"""The Analyst's insights have to tell the truth about why nothing was sent.

Manual approval means drafts wait for a human. Calling that BLOCKED sends
people hunting for a broken API key that was never the problem.
"""

from sellbird.agents.analyst import Analyst


def report(outbox=None, queued=0, contacted=0):
    return {
        "pipeline": {"new": 10, "queued": queued, "contacted": contacted},
        "campaigns": [],
        "intents": {},
        "stages": {},
        "outbox": outbox or {},
    }


def insights(outbox=None, queued=0, contacted=0):
    return Analyst._generate_insights(Analyst, report(outbox, queued, contacted))


def test_drafts_waiting_for_approval_are_human_needed_not_blocked():
    lines = insights({"pending_review": 12}, queued=12)
    joined = " ".join(lines)
    assert "HUMAN NEEDED" in joined
    assert "BLOCKED" not in joined
    assert "12 draft(s)" in joined


def test_approved_but_unsent_is_blocked():
    lines = insights({"approved": 3}, queued=3)
    joined = " ".join(lines)
    assert "BLOCKED" in joined
    assert "approved but nothing has been sent" in joined


def test_queued_with_nothing_in_the_outbox_is_blocked():
    lines = insights({}, queued=4)
    joined = " ".join(lines)
    assert "BLOCKED" in joined
    assert "no draft is waiting for approval" in joined


def test_nothing_queued_says_nothing_about_being_blocked():
    lines = insights({}, queued=0)
    joined = " ".join(lines)
    assert "BLOCKED" not in joined
    assert "HUMAN NEEDED" not in joined


def test_sent_mail_is_never_called_blocked():
    lines = insights({"sent": 40}, queued=0, contacted=40)
    joined = " ".join(lines)
    assert "BLOCKED" not in joined
