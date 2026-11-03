"""Checkout safety and quota recovery; all requests and sending are mocked."""
from urllib.parse import quote, unquote
import httpx
import pytest
from sellbird.integrations.precleared import PreclearedClient, SETTINGS_PREFIX, BUY_URL
from tests.test_precleared import state, queue, with_check, OK_BODY
from tests.test_outbox_native import seed_prospect, FakeProvider, make_sender

@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    'https://',
    'https:///buy',
    'https://?checkout=123',
    'https://precleared.dev:invalid/buy',
    'https://precleared.dev:65536/buy',
    'https://precleared.dev:/buy',
    'https://precleared.dev:0/buy',
    'https://user@precleared.dev/buy',
    'https://user:password@precleared.dev/buy',
    'https://precleared.dev\\@evil.example/buy',
    'https://%70recleared.dev/buy',
    'https://[not-ipv6]/buy',
    'https://bad_host.example/buy',
    'https://-bad.example/buy',
    'https://a..example/buy',
    'https://[::1]extra/buy',
])
async def test_malformed_https_link_uses_fallback(state, url):
    client = PreclearedClient("GB", api_key="pc_test_review", transport=httpx.MockTransport(
        lambda r: httpx.Response(402, json={"buy_url": url})))
    result = await client.clear("person@example.com", state)
    assert result.held and not result.allowed
    assert f"add a pack ({BUY_URL})" in result.reason

@pytest.mark.asyncio
async def test_402_holds_sequences_allows_reply_then_recovers_next_cycle(state):
    first = await seed_prospect(state)
    second = await seed_prospect(state, email="sam@beta.com")
    reply = await seed_prospect(state, email="ann@gamma.com", status="replied")
    await queue(state, first)
    await queue(state, second, to="sam@beta.com")
    await queue(state, reply, kind="reply", to="ann@gamma.com")
    calls = []
    def limited(request):
        calls.append(request)
        return httpx.Response(402, json={"buy_url": "https://checkout.stripe.com/c/pay/example"})
    provider = FakeProvider()
    sender = with_check(make_sender(state, provider), httpx.MockTransport(limited), api_key="pc_test_review")
    await sender._drain_due()
    assert len(calls) == 1
    assert [m["to"] for m in provider.sent] == ["ann@gamma.com"]
    assert len(await state.get_outbox(status="approved")) == 2
    assert not await state.get_outbox(status="failed")
    assert await state.get_setting(SETTINGS_PREFIX + "jane@acme.com") == ""
    sender.precleared._transport = httpx.MockTransport(lambda r: httpx.Response(200, json=OK_BODY))
    await sender._drain_due()
    assert sorted(m["to"] for m in provider.sent) == ["ann@gamma.com", "jane@acme.com", "sam@beta.com"]
    assert not await state.get_outbox(status="approved")

@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["literal", "upper", "lower", "mixed", "nested", "excessive"])
@pytest.mark.parametrize("location", ["path", "query", "fragment"])
async def test_encoded_key_not_exposed_in_checkout_reason(state, encoding, location):
    key = "pc_live_secret123"
    encoded = "".join("%{:02X}".format(ord(c)) for c in key)
    if encoding == "literal":
        encoded = key
    elif encoding == "lower":
        encoded = encoded.lower()
    elif encoding == "mixed":
        encoded = "".join(c.lower() if i % 2 else c for i, c in enumerate(encoded))
    elif encoding in ("nested", "excessive"):
        for _ in range(2 if encoding == "nested" else 10):
            encoded = quote(encoded, safe="")
    checkout = "https://checkout.stripe.com/" + {
        "path": encoded, "query": "pay?key=" + encoded, "fragment": "pay#" + encoded,
    }[location]
    client = PreclearedClient("GB", api_key=key, transport=httpx.MockTransport(
        lambda r: httpx.Response(402, json={"buy_url": checkout})))
    result = await client.clear("person@example.com", state)
    assert f"add a pack ({BUY_URL})" in result.reason
    assert key not in unquote(result.reason)
    assert await state.get_setting(SETTINGS_PREFIX + "person@example.com") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("checkout", [
    "https://checkout.stripe.com/c/pay/cs_test_example123#fidexample",
    "https://checkout.stripe.com:443/c/pay/cs_test_example123?return=a%2Fb#fid%23example",
    "https://checkout.stripe.com/c/pay/pc_live_unrelated456#fidexample",
])
async def test_valid_checkout_is_preserved_exactly(state, checkout):
    client = PreclearedClient("GB", api_key="pc_live_secret123", transport=httpx.MockTransport(
        lambda r: httpx.Response(402, json={"buy_url": checkout})))
    result = await client.clear("person@example.com", state)
    assert result.held and not result.allowed
    assert f"add a pack ({checkout})" in result.reason
