"""A reply from anyone at a company stops the sequence to everyone there.

Stop-on-reply used to be per person: the owner wrote back "not interested"
and the office manager got the follow-up the next morning. Colleagues are
the same company (company_id), or, for people Harvey has no company for,
the same email domain, never a shared free-mail one.
"""

import pytest

from harvey.agents.sender import COLLEAGUE_REPLIED
from harvey.state import Company, Prospect
from tests.test_outbox_native import (  # noqa: F401  (fixture)
    FakeProvider, InboundMessage, _now_iso, make_handler, make_sender,
    seed_campaign, state,
)


async def _person(state, email, company_id="", status="contacted"):
    first = email.split("@")[0].title()
    return await state.add_prospect(Prospect(
        first_name=first, last_name="Doe", title="VP", company="Acme",
        company_id=company_id, email=email, email_status="verified",
        email_verified=True, status=status,
    ))


async def _queued(state, pid, email, kind="sequence", status="approved", step=2):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b",
        send_at=_now_iso(), status=status, campaign_id="c1", step=step, kind=kind,
    )


async def _reply(state, from_email, body="Who are you exactly?", intent="question"):
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in-" + from_email, from_email=from_email,
        subject="Re: hi", body=body,
    )]
    await make_handler(state, provider, intent=intent)._run_native()


async def _status(state, item_id):
    [row] = [r for r in await state.get_outbox() if r["id"] == item_id]
    return row["status"], row.get("error") or ""


async def _company(state, domain="acme.com"):
    return await state.add_company(Company(name="Acme", domain=domain))


# ── the handler ──


@pytest.mark.asyncio
@pytest.mark.parametrize("body,intent", [
    ("Who are you exactly?", "question"),
    ("Not interested, thanks.", "not_interested"),
    ("Please remove me from your list.", "question"),          # an opt-out
    ("Sounds good, send times.", "interested"),
])
async def test_any_reply_stops_colleagues(state, body, intent):
    cid = await _company(state)
    ann = await _person(state, "ann@acme.com", cid)
    bob = await _person(state, "bob@acme.com", cid)
    queued = await _queued(state, bob, "bob@acme.com")
    waiting = await _queued(state, bob, "bob@acme.com", status="pending_review", step=3)
    await _reply(state, "ann@acme.com", body, intent)
    for item in (queued, waiting):
        status, error = await _status(state, item)
        assert status == "cancelled" and COLLEAGUE_REPLIED in error
    assert (await state.get_prospect(ann)).status != "contacted"


@pytest.mark.asyncio
async def test_other_companies_and_replies_to_colleagues_are_untouched(state):
    acme, other = await _company(state), await _company(state, "other.com")
    await _person(state, "ann@acme.com", acme)
    bob = await _person(state, "bob@acme.com", acme)
    cat = await _person(state, "cat@other.com", other)
    answer = await _queued(state, bob, "bob@acme.com", kind="reply", step=1)
    elsewhere = await _queued(state, cat, "cat@other.com")
    await _reply(state, "ann@acme.com")
    assert (await _status(state, answer))[0] == "approved"     # Bob wrote to us himself
    assert (await _status(state, elsewhere))[0] == "approved"


@pytest.mark.asyncio
async def test_without_a_company_the_email_domain_decides(state):
    await _person(state, "ann@acme.com")
    bob = await _person(state, "bob@acme.com")
    queued = await _queued(state, bob, "bob@acme.com")
    await _reply(state, "ann@acme.com")
    assert (await _status(state, queued))[0] == "cancelled"


@pytest.mark.asyncio
async def test_a_shared_free_mail_domain_is_not_a_company(state):
    await _person(state, "ann@gmail.com")
    bob = await _person(state, "bob@gmail.com")
    queued = await _queued(state, bob, "bob@gmail.com")
    await _reply(state, "ann@gmail.com")
    assert (await _status(state, queued))[0] == "approved"


@pytest.mark.asyncio
async def test_a_bounce_does_not_stop_colleagues(state):
    cid = await _company(state)
    ann = await _person(state, "ann@acme.com", cid)
    bob = await _person(state, "bob@acme.com", cid)
    sent = await _queued(state, ann, "ann@acme.com", step=1)
    await state.update_outbox_item(sent, status="sent", message_id="<m1@x>")
    queued = await _queued(state, bob, "bob@acme.com")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@acme.com", subject="Undeliverable",
        body="Delivery failed", is_bounce=True, in_reply_to="<m1@x>",
    )]
    await make_handler(state, provider)._run_native()
    assert (await _status(state, queued))[0] == "approved"


# ── the sender: what was staged before, or is staged after ──


@pytest.mark.asyncio
async def test_the_drain_cancels_a_colleague_s_follow_up(state):
    cid = await _company(state)
    ann = await _person(state, "ann@acme.com", cid, status="replied")
    bob = await _person(state, "bob@acme.com", cid)
    await state.stop_company(ann)
    queued = await _queued(state, bob, "bob@acme.com")
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    status, error = await _status(state, queued)
    assert status == "cancelled" and COLLEAGUE_REPLIED in error


@pytest.mark.asyncio
async def test_a_new_campaign_skips_people_whose_colleague_replied(state):
    cid = await _company(state)
    ann = await _person(state, "ann@acme.com", cid, status="lost")
    await state.stop_company(ann)
    bob = await _person(state, "bob@acme.com", cid, status="new")
    dee = await _person(state, "dee@else.com", status="new")
    campaign = await seed_campaign(state, [bob, dee])
    await make_sender(state, FakeProvider())._stage_campaign_native(campaign)
    staged = {r["to_email"] for r in await state.get_outbox()}
    assert staged == {"dee@else.com"}


# ── review: only real colleagues, only real answers ──


@pytest.mark.asyncio
async def test_two_companies_on_one_mail_domain_are_not_colleagues(state):
    a, b = await _company(state, "annroofing.com"), await _company(state, "bobplumbing.com")
    await _person(state, "ann@acme.com", a)
    bob = await _person(state, "bob@acme.com", b)
    queued = await _queued(state, bob, "bob@acme.com")
    await _reply(state, "ann@acme.com")
    assert (await _status(state, queued))[0] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["rr.com", "yahoo.co.uk", "earthlink.net", "btinternet.com", "shaw.ca", "pm.me"])
async def test_isp_and_regional_mailboxes_are_not_a_company(state, domain):
    await _person(state, f"ann@{domain}")
    bob = await _person(state, f"bob@{domain}")
    queued = await _queued(state, bob, f"bob@{domain}")
    await _reply(state, f"ann@{domain}")
    assert (await _status(state, queued))[0] == "approved"


@pytest.mark.asyncio
async def test_one_side_without_a_company_matches_only_that_company_s_domain(state):
    acme = await _company(state, "acme.com")
    await _person(state, "ann@acme.com", acme)
    bob = await _person(state, "bob@acme.com")                 # no company on file
    queued = await _queued(state, bob, "bob@acme.com")
    other = await _company(state, "agency.com")               # their mail isn't on their domain
    cat = await _person(state, "cat@shared.net", other)
    dan = await _person(state, "dan@shared.net")
    dans = await _queued(state, dan, "dan@shared.net")
    await _reply(state, "ann@acme.com")
    await _reply(state, "cat@shared.net")
    assert (await _status(state, queued))[0] == "cancelled"
    assert (await _status(state, dans))[0] == "approved"
    assert cat


@pytest.mark.asyncio
async def test_an_address_with_no_domain_matches_nobody(state):
    ann = await _person(state, "ann@")
    bob = await _person(state, "bob@")
    queued = await _queued(state, bob, "bob@")
    await state.stop_company(ann)
    assert (await _status(state, queued))[0] == "approved"
    assert await state.colleague_who_replied(bob) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,body", [
    ("ooo", "I'm out of the office until Monday."),
    ("wrong_person", "Not me, talk to Bob."),
])
async def test_an_out_of_office_or_a_referral_does_not_stop_the_company(state, intent, body):
    cid = await _company(state)
    await _person(state, "ann@acme.com", cid)
    bob = await _person(state, "bob@acme.com", cid)
    queued = await _queued(state, bob, "bob@acme.com")
    await _reply(state, "ann@acme.com", body, intent)
    assert (await _status(state, queued))[0] == "approved"
    assert await state.colleague_who_replied(bob) is None


@pytest.mark.asyncio
async def test_the_writer_skips_and_doesn_t_count_people_whose_company_answered(state):
    cid = await _company(state)
    ann = await _person(state, "ann@acme.com", cid)
    await _person(state, "bob@acme.com", cid, status="new")
    assert await state.count_writable_prospects() == 1
    await state.stop_company(ann)
    assert await state.count_writable_prospects() == 0


@pytest.mark.asyncio
async def test_instantly_campaigns_stop_for_the_whole_company():
    from harvey.integrations.instantly import InstantlyClient
    client = InstantlyClient.__new__(InstantlyClient)
    sent = {}

    async def fake_request(method, path, body=None, **kw):
        sent.update(body or {})
        return {"id": "c1"}
    client._request = fake_request
    await client.create_campaign("x")
    assert sent["stop_for_company"] is True
