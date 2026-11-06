"""Addresses and domains on `compliance.do_not_contact` never get an email.

Competitors, existing customers, anyone under a legal hold. The gate is the
last word: it holds at the final moment before a send, including for an email
a human already approved in the Outbox. Discovery applies the same list
earlier, so a forbidden company never becomes a row — but only the gate can
stop a send, so neither check replaces the other.
"""

from types import SimpleNamespace

import pytest

from sellbird.config import ComplianceConfig
from sellbird.gate import (
    do_not_contact_domain_match, do_not_contact_match, pre_send_check,
)
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_prospect, state,
)

BODY = "A short, plain note. Question?"


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _prospect(email):
    return SimpleNamespace(email=email, email_status="verified")


def _check(to_email, entries, kind="sequence"):
    return pre_send_check(
        to_email, "hello there", BODY, prospect=_prospect(to_email),
        kind=kind, do_not_contact=entries,
    )


# ── Matching ──

def test_a_domain_blocks_every_address_at_it():
    assert do_not_contact_match("jane@acme.example", ["acme.example"]) == "acme.example"
    assert do_not_contact_match("rob@acme.example", ["acme.example"]) == "acme.example"


def test_a_domain_covers_its_subdomains():
    assert do_not_contact_match("jane@mail.acme.example", ["acme.example"]) == "acme.example"


def test_a_lookalike_domain_is_not_blocked():
    """The boundary that stops a blocklist overreaching: a plain suffix test
    would block this, and it is a different company."""
    assert do_not_contact_match("jane@notacme.example", ["acme.example"]) == ""
    assert do_not_contact_match("jane@acme.example.evil.test", ["acme.example"]) == ""


def test_an_entry_with_an_at_blocks_only_that_address():
    entries = ["jane@acme.example"]
    assert do_not_contact_match("jane@acme.example", entries) == "jane@acme.example"
    assert do_not_contact_match("rob@acme.example", entries) == ""


def test_an_empty_list_blocks_nothing():
    assert do_not_contact_match("jane@acme.example", []) == ""
    assert do_not_contact_match("jane@acme.example", ()) == ""


def test_entries_are_normalised_by_the_config():
    cfg = ComplianceConfig(do_not_contact=[" @Acme.EXAMPLE ", "www.other.example", "", "acme.example"])
    # lowercased, @ and www. stripped, blanks and duplicates dropped
    assert cfg.do_not_contact == ["acme.example", "other.example"]


def test_a_single_string_is_accepted_as_a_list_of_one():
    assert ComplianceConfig(do_not_contact="acme.example").do_not_contact == ["acme.example"]


def test_a_hand_built_tuple_is_normalised_too():
    """Callers outside the config should behave the same way."""
    assert do_not_contact_match("jane@acme.example", ("  ACME.Example ",)) == "acme.example"


# ── The gate ──

def test_the_gate_blocks_a_listed_recipient():
    result = _check("jane@acme.example", ["acme.example"])
    assert not result
    assert any("do-not-contact" in r for r in result.reasons)


def test_the_gate_passes_an_unlisted_recipient():
    assert _check("jane@other.example", ["acme.example"])


def test_replies_are_blocked_too():
    """A reply is still contact. The operator can answer by hand."""
    assert not _check("jane@acme.example", ["acme.example"], kind="reply")


def test_nothing_changes_when_the_list_is_empty():
    assert _check("jane@acme.example", [])


# ── End to end ──

@pytest.mark.asyncio
async def test_an_approved_email_to_a_listed_domain_never_leaves(state):
    """The point of putting this in the gate: approval does not override it."""
    pid = await seed_prospect(state, email="jane@acme.example")
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.example", subject="hello there",
        body=BODY, send_at=_now_iso(), status="approved", kind="reply",
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.compliance = ComplianceConfig(do_not_contact=["acme.example"])

    await sender._drain_due()

    assert provider.sent == [], "an email went to a do-not-contact domain"
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "failed"
    assert "do-not-contact" in item["error"]


@pytest.mark.asyncio
async def test_an_unlisted_recipient_still_sends(state):
    """The guard must not cost everyone else their mail."""
    pid = await seed_prospect(state, email="jane@other.example")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@other.example", subject="hello there",
        body=BODY, send_at=_now_iso(), status="approved", kind="reply",
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.compliance = ComplianceConfig(do_not_contact=["acme.example"])

    await sender._drain_due()

    assert len(provider.sent) == 1


# ── Entries pasted the way people copy them ──

@pytest.mark.parametrize("pasted", [
    "https://acme.example/",
    "http://www.acme.example/about",
    "acme.example/contact",
    "*.acme.example",
    "acme.example:443",
])
def test_a_pasted_url_or_wildcard_still_blocks_the_domain(pasted):
    """A URL copied from the browser bar must not silently match nothing."""
    cfg = ComplianceConfig(do_not_contact=[pasted])
    assert cfg.do_not_contact == ["acme.example"]
    assert do_not_contact_match("jane@acme.example", cfg.do_not_contact) == "acme.example"


def test_a_mailto_link_is_one_address():
    cfg = ComplianceConfig(do_not_contact=["mailto:Jane@Acme.example"])
    assert cfg.do_not_contact == ["jane@acme.example"]


# ── Instantly has no Outbox, so the list is applied before anything leaves ──

@pytest.mark.asyncio
async def test_listed_leads_are_left_out_of_the_instantly_upload(state):
    from sellbird.agents.sender import Sender
    from sellbird.models.prospect import Prospect
    from tests.test_outbox_native import Cfg, Env
    cfg = Cfg()
    cfg.compliance = ComplianceConfig(do_not_contact=["acme.example"])
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    kept = Prospect(first_name="Al", last_name="B", company="Co", email="al@other.example")
    listed = Prospect(first_name="Jo", last_name="C", company="Acme", email="jo@acme.example")
    assert sender._reviewed_leads([kept, listed]) == [kept]


@pytest.mark.asyncio
async def test_an_instantly_reply_to_a_listed_address_goes_to_you(state):
    from sellbird.agents.handler import Handler
    from tests.test_outbox_native import Cfg, Env
    cfg = Cfg()
    cfg.compliance = ComplianceConfig(do_not_contact=["acme.example"])
    handler = Handler(brain=None, state=state, config=cfg, env=Env())
    held = handler._instantly_reply_held(_prospect("jane@acme.example"), BODY)
    assert any("do-not-contact" in r for r in held)


# ── Review round: the list must hold however it is written and whenever ──

def test_a_comma_or_space_separated_string_is_several_entries():
    cfg = ComplianceConfig(do_not_contact="acme.example, other.example  third.example")
    assert cfg.do_not_contact == ["acme.example", "other.example", "third.example"]


def test_a_name_and_address_pasted_from_a_mail_client():
    cfg = ComplianceConfig(do_not_contact=["Jane Doe <Jane@Acme.example>"])
    assert cfg.do_not_contact == ["jane@acme.example"]


def test_a_wildcard_mailbox_is_the_domain():
    assert ComplianceConfig(do_not_contact=["*@acme.example"]).do_not_contact == ["acme.example"]


def test_invisible_characters_and_unicode_dots_are_cleaned():
    cfg = ComplianceConfig(do_not_contact=["﻿acme.example​", "other．example",
                                           "third。example"])
    assert cfg.do_not_contact == ["acme.example", "other.example", "third.example"]


def test_an_international_domain_matches_its_ascii_form():
    cfg = ComplianceConfig(do_not_contact=["bücher.example"])
    assert cfg.do_not_contact == ["xn--bcher-kva.example"]
    assert do_not_contact_match("jo@xn--bcher-kva.example", cfg.do_not_contact)


def test_an_address_entry_covers_its_plus_tags():
    assert do_not_contact_match("jane+news@acme.example", ["jane@acme.example"])
    assert not do_not_contact_match("janet@acme.example", ["jane@acme.example"])


def test_www_is_not_stripped_from_a_mailbox():
    assert not do_not_contact_match("www.jane@acme.example", ["jane@acme.example"])


@pytest.mark.parametrize("bad", [5, True, [{"domain": "acme.example"}], ["not a domain"],
                                 ["www.example"], ["example"]])
def test_an_entry_that_names_nothing_is_a_config_error(bad):
    """Silently matching nothing is the worst outcome: the user thinks the
    company is protected. A clear error at load time instead."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ComplianceConfig(do_not_contact=bad)


def test_a_running_sellbird_picks_up_an_edit_to_the_list(tmp_path):
    import shutil
    from sellbird.config import live_do_not_contact, load_config
    from sellbird.paths import PROJECT_ROOT
    path = tmp_path / "sellbird.yaml"
    shutil.copy(PROJECT_ROOT / "sellbird.yaml", path)
    config = load_config(str(path))
    assert live_do_not_contact(config) == []
    text = path.read_text().replace("  do_not_contact: []", '  do_not_contact: ["acme.example"]')
    assert text != path.read_text()
    path.write_text(text)
    assert live_do_not_contact(config) == ["acme.example"]
    # A typo while Sellbird runs keeps the last good list, never an empty one.
    path.write_text(text.replace('["acme.example"]', "[5"))
    assert live_do_not_contact(config) == ["acme.example"]


@pytest.mark.asyncio
async def test_an_approved_cold_email_to_a_listed_domain_never_leaves(state):
    pid = await seed_prospect(state, email="jane@acme.example")
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.example", subject="hello there",
        body=BODY, send_at=_now_iso(), status="approved", kind="sequence",
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.compliance = ComplianceConfig(do_not_contact=["acme.example"])
    await sender._drain_due()
    assert provider.sent == []
    assert "do-not-contact" in (await state.get_outbox_item(item_id))["error"]


@pytest.mark.asyncio
async def test_export_leaves_out_listed_prospects(state, monkeypatch):
    from sellbird import export
    from sellbird.models.prospect import Prospect
    for email in ("jane@acme.example", "al@other.example"):
        await state.add_prospect(Prospect(first_name="A", last_name="B", company="Co",
                                          email=email, email_status="verified"))
    cfg = SimpleNamespace(compliance=ComplianceConfig(do_not_contact=["acme.example"]))
    rows = await export.collect_prospects(state, config=cfg)
    assert [r["email"] for r in rows] == ["al@other.example"]


@pytest.mark.asyncio
async def test_a_reply_from_a_listed_address_goes_to_you_without_a_draft(state, monkeypatch):
    """Nothing can be sent to them, so spend no model call drafting, and make
    sure a human sees that they wrote."""
    from sellbird.agents.handler import Handler
    from tests.test_outbox_native import Cfg, Env
    pid = await seed_prospect(state, email="jane@acme.example", status="contacted")
    cfg = Cfg()
    cfg.compliance = ComplianceConfig(do_not_contact=["acme.example"])
    handler = Handler(brain=None, state=state, config=cfg, env=Env())
    handler.provider = FakeProvider()
    drafted = []

    async def _gen(*a, **k):
        drafted.append(1)
        return "Happy to help."

    async def _intent(*a, **k):
        return "question"
    monkeypatch.setattr(handler, "_generate_response", _gen)
    monkeypatch.setattr(handler, "_classify_intent", _intent)
    await handler._process_reply("jane@acme.example", "What does it cost?", "u1")
    assert drafted == []
    assert [c.prospect_id for c in await state.get_conversations_by_status("needs_human")] == [pid]


# ── Discovery matches on the domain, because that is all it has ──


@pytest.mark.parametrize("domain", [
    "acme.example", "www.acme.example", "ACME.example", "  acme.example  ",
    "mail.acme.example", "a.b.acme.example",
])
def test_a_listed_domain_is_recognised_however_it_arrives(domain):
    assert do_not_contact_domain_match(domain, ["acme.example"]) == "acme.example"


@pytest.mark.parametrize("domain", [
    "notacme.example",          # the suffix test a blocklist usually gets wrong
    "acme.example.evil.test",   # the listed name as a leading label
    "other.example", "", "   ",
])
def test_a_lookalike_domain_is_left_alone(domain):
    assert do_not_contact_domain_match(domain, ["acme.example"]) == ""


def test_an_address_entry_does_not_exclude_its_company():
    """"jane@acme.example" bars one mailbox, not the company behind it. A
    colleague is still worth reaching, so the company stays discoverable and
    only the gate stops mail to Jane."""
    assert do_not_contact_domain_match("acme.example", ["jane@acme.example"]) == ""


def test_address_and_domain_entries_coexist():
    entries = ["jane@other.example", "acme.example"]
    assert do_not_contact_domain_match("acme.example", entries) == "acme.example"
    assert do_not_contact_domain_match("other.example", entries) == ""


def test_an_entry_pasted_as_a_url_still_covers_the_domain():
    assert do_not_contact_domain_match(
        "acme.example", ["https://www.acme.example/about"]) == "acme.example"


def test_an_empty_list_matches_nothing():
    assert do_not_contact_domain_match("acme.example", []) == ""
