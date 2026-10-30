"""Microsoft 365 provider: token rotation, draft-then-send, threaded replies,
reading the inbox without touching it, and failing loudly."""

import base64
import json
import os

import httpx
import pytest

from harvey.integrations import microsoft
from harvey.integrations.microsoft import GraphError, MicrosoftProvider


class Env:
    m365_client_id = "client-123"
    m365_tenant_id = "tenant-abc"


class Graph:
    """A tiny fake of the Graph + login endpoints Harvey uses."""

    def __init__(self):
        self.requests = []
        self.inbox = []
        self.fail = {}          # (method, path suffix) -> status
        self.drafts = 0
        self.pages = {}         # inbox page number -> httpx.Response, or a dict
                                # merged into that page's normal JSON body

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        path = request.url.path
        method = request.method
        for (m, suffix), status in self.fail.items():
            if m == method and path.endswith(suffix):
                return httpx.Response(status, json={"error": {"code": "boom"}})
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={
                "access_token": "at-1", "expires_in": 3600, "refresh_token": "rt-2",
            })
        if path == "/v1.0/me":
            return httpx.Response(200, json={"mail": "Owner@sender.example"})
        if path == "/v1.0/me/messages" and method == "POST":
            self.drafts += 1
            return httpx.Response(201, json={
                "id": f"draft{self.drafts}",
                "internetMessageId": f"<draft{self.drafts}@sender.example>",
                "conversationId": "conv1",
            })
        if path.endswith("/createReply"):
            self.drafts += 1
            return httpx.Response(201, json={
                "id": f"reply{self.drafts}",
                "internetMessageId": f"<reply{self.drafts}@sender.example>",
                "conversationId": "conv9",
            })
        if path.endswith("/attachments") and method == "POST":
            return httpx.Response(201, json={"id": "att1"})
        if method == "PATCH" or method == "DELETE":
            return httpx.Response(200 if method == "PATCH" else 204, json={} if method == "PATCH" else None)
        if path.endswith("/send"):
            return httpx.Response(202)
        if path == "/v1.0/me/mailFolders/inbox/messages":
            return self._inbox_page(request)
        return httpx.Response(404, json={"error": {"code": "notFound"}})

    def _inbox_page(self, request):
        """Pages like Graph: $top per page, then an opaque nextLink whose
        query carries only $skip (and a token) — not the first page's params."""
        params = request.url.params
        top = int(params.get("$top") or params.get("t") or 50)
        skip = int(params.get("$skip", 0))
        page = skip // top + 1
        body = {"value": self.inbox[skip:skip + top]}
        if skip + top < len(self.inbox):
            body["@odata.nextLink"] = (
                "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
                f"?%24skip={skip + top}&t={top}&%24skiptoken=opaque{page}"
            )
        override = self.pages.get(page)
        if isinstance(override, httpx.Response):
            return override
        return httpx.Response(200, json={**body, **(override or {})})

    def calls(self):
        return [(r.method, r.url.path) for r in self.requests
                if r.url.host == "graph.microsoft.com"]

    def inbox_requests(self):
        return [r for r in self.requests if r.url.path.endswith("/inbox/messages")]


def message(i, sender="someone@elsewhere.example", body="hello"):
    return {"id": f"m{i}", "subject": f"note {i}",
            "from": {"emailAddress": {"address": sender}},
            "body": {"content": body}, "internetMessageId": f"<{i}@elsewhere.example>",
            "receivedDateTime": "2026-10-14T15:00:00Z"}


@pytest.fixture
def graph(tmp_path, monkeypatch):
    token_file = tmp_path / "m365_token.json"
    token_file.write_text(json.dumps({"refresh_token": "rt-1", "tenant": "tenant-abc"}))
    monkeypatch.setattr(microsoft, "TOKEN_FILE", token_file)
    return Graph()


def provider(graph):
    return MicrosoftProvider(config=None, env=Env(), transport=httpx.MockTransport(graph))


@pytest.mark.asyncio
async def test_refresh_token_rotates_and_is_saved(graph):
    p = provider(graph)
    await p._token()

    [login] = [r for r in graph.requests if r.url.host == "login.microsoftonline.com"]
    assert login.url.path == "/tenant-abc/oauth2/v2.0/token"
    form = dict(x.split("=", 1) for x in login.content.decode().split("&"))
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-1"
    assert "client_secret" not in form  # public client
    saved = json.loads(microsoft.TOKEN_FILE.read_text())
    assert saved["refresh_token"] == "rt-2"
    if os.name == "posix":  # Windows has no POSIX mode bits; ACLs apply
        assert microsoft.TOKEN_FILE.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_new_email_is_drafted_then_sent_as_plain_text(graph):
    result = await provider(graph).send_email("jane@acme.com", "booking", "Hi Jane.")

    assert result.ok
    assert result.message_id == "<draft1@sender.example>"  # bounces match on this
    assert graph.calls() == [
        ("GET", "/v1.0/me"),  # which mailbox the token belongs to
        ("POST", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages/draft1/send"),
    ]
    [draft] = [json.loads(r.content) for r in graph.requests
               if r.method == "POST" and r.url.path == "/v1.0/me/messages"]
    assert draft["body"] == {"contentType": "Text", "content": "Hi Jane."}
    assert draft["toRecipients"] == [{"emailAddress": {"address": "jane@acme.com"}}]


@pytest.mark.asyncio
async def test_reply_is_threaded_with_create_reply(graph):
    result = await provider(graph).send_email(
        "jane@acme.com", "Re: booking", "Tuesday works.",
        thread_ref="AAMk-their-message", in_reply_to="<r1@acme.com>",
    )

    assert result.ok and result.message_id == "<reply1@sender.example>"
    assert graph.calls() == [
        ("GET", "/v1.0/me"),
        ("POST", "/v1.0/me/messages/AAMk-their-message/createReply"),
        ("PATCH", "/v1.0/me/messages/reply1"),
        ("POST", "/v1.0/me/messages/reply1/send"),
    ]
    [patch] = [json.loads(r.content) for r in graph.requests if r.method == "PATCH"]
    assert patch["body"]["content"] == "Tuesday works."
    assert patch["toRecipients"][0]["emailAddress"]["address"] == "jane@acme.com"


@pytest.mark.asyncio
async def test_reply_to_a_deleted_message_is_sent_unthreaded(graph):
    graph.fail[("POST", "/createReply")] = 404
    result = await provider(graph).send_email(
        "jane@acme.com", "Re: booking", "Tuesday works.", thread_ref="gone",
    )

    assert result.ok
    assert ("POST", "/v1.0/me/messages") in graph.calls()


@pytest.mark.asyncio
async def test_failed_send_deletes_the_draft(graph):
    graph.fail[("POST", "/send")] = 500
    result = await provider(graph).send_email("jane@acme.com", "booking", "Hi.")

    assert not result.ok
    assert ("DELETE", "/v1.0/me/messages/draft1") in graph.calls()


def signed_config(logo):
    from types import SimpleNamespace
    from harvey.config import SignatureConfig
    return SimpleNamespace(
        persona=SimpleNamespace(email="owner@sender.example", company="Acme"),
        channels=SimpleNamespace(email=SimpleNamespace(
            signature=SignatureConfig(logo=str(logo), direction="rtl"))),
    )


def signed_provider(graph, logo):
    return MicrosoftProvider(config=signed_config(logo), env=Env(),
                             transport=httpx.MockTransport(graph))


@pytest.mark.asyncio
async def test_signature_sends_html_with_the_logo_inline(graph, tmp_path):
    from tests.test_signature import png
    logo = tmp_path / "logo.png"
    logo.write_bytes(png(200, 100))

    result = await signed_provider(graph, logo).send_email("jane@acme.com", "booking", "Hi Jane.")

    assert result.ok
    assert graph.calls() == [
        ("GET", "/v1.0/me"),
        ("POST", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages/draft1/attachments"),  # before it's sent
        ("POST", "/v1.0/me/messages/draft1/send"),
    ]
    posts = [json.loads(r.content) for r in graph.requests
             if r.url.host == "graph.microsoft.com" and r.method == "POST" and r.content]
    draft, attachment = posts[0], posts[1]
    assert draft["body"]["contentType"] == "HTML"
    assert "Hi Jane." in draft["body"]["content"]
    assert f'src="cid:{attachment["contentId"]}"' in draft["body"]["content"]
    assert attachment["isInline"] is True
    assert attachment["contentType"] == "image/png"
    assert base64.b64decode(attachment["contentBytes"]) == logo.read_bytes()


@pytest.mark.asyncio
async def test_signed_reply_carries_the_logo_too(graph, tmp_path):
    from tests.test_signature import png
    logo = tmp_path / "logo.png"
    logo.write_bytes(png(200, 100))

    result = await signed_provider(graph, logo).send_email(
        "jane@acme.com", "Re: booking", "Tuesday works.", thread_ref="AAMk-their-message")

    assert result.ok
    assert graph.calls()[-2:] == [
        ("POST", "/v1.0/me/messages/reply1/attachments"),
        ("POST", "/v1.0/me/messages/reply1/send"),
    ]
    [patch] = [json.loads(r.content) for r in graph.requests if r.method == "PATCH"]
    assert patch["body"]["contentType"] == "HTML"


@pytest.mark.asyncio
async def test_unusable_logo_sends_nothing(graph, tmp_path):
    result = await signed_provider(graph, tmp_path / "missing.png").send_email(
        "jane@acme.com", "booking", "Hi Jane.")

    assert not result.ok and result.error.startswith("signature:")
    assert graph.calls() == []  # no draft, so nothing to clean up either


@pytest.mark.asyncio
async def test_inbox_is_read_without_changing_it(graph):
    graph.inbox = [
        {"id": "m1", "subject": "Re: booking",
         "from": {"emailAddress": {"address": "Jane@Acme.com"}},
         "body": {"contentType": "text", "content": "Sounds good."},
         "internetMessageId": "<r1@acme.com>", "conversationId": "c1",
         "receivedDateTime": "2026-10-14T15:00:00Z",
         "internetMessageHeaders": [{"name": "In-Reply-To", "value": "<draft1@sender.example>"}]},
        {"id": "m2", "subject": "Undeliverable: booking",
         "from": {"emailAddress": {"address": "postmaster@sender.example"}},
         "body": {"content": "couldn't be delivered"},
         "internetMessageHeaders": [{"name": "In-Reply-To", "value": "<draft2@sender.example>"}]},
        {"id": "m3", "subject": "note to self",
         "from": {"emailAddress": {"address": "owner@sender.example"}}, "body": {"content": "x"}},
    ]
    replies = await provider(graph).get_replies()

    assert [m.provider_id for m in replies] == ["m1", "m2"]  # own mail skipped
    jane, bounce = replies
    assert jane.from_email == "jane@acme.com"
    assert jane.body == "Sounds good."
    assert jane.thread_ref == "m1"           # what createReply needs
    assert jane.in_reply_to == "<draft1@sender.example>"
    assert bounce.is_bounce and bounce.in_reply_to == "<draft2@sender.example>"

    listing = [r for r in graph.requests if r.url.path.endswith("/inbox/messages")][0]
    assert listing.headers["Prefer"] == 'outlook.body-content-type="text"'
    assert "receivedDateTime ge" in listing.url.params["$filter"]
    assert all(m in ("GET",) for m, _ in graph.calls())  # nothing marked read


@pytest.mark.asyncio
async def test_unreadable_inbox_raises_so_sends_are_held(graph):
    graph.fail[("GET", "/inbox/messages")] = 503
    with pytest.raises(GraphError):
        await provider(graph).get_replies()


@pytest.mark.asyncio
async def test_not_authorized_raises(graph):
    microsoft.TOKEN_FILE.unlink()
    p = provider(graph)
    assert not p.is_configured()
    with pytest.raises(GraphError, match="harvey microsoft auth"):
        await p.get_replies()


@pytest.mark.asyncio
async def test_handler_holds_sends_when_graph_is_down(graph, tmp_path):
    """End to end with the handler: a Graph outage is a failed read."""
    import os
    from harvey.agents.handler import Handler
    from harvey.state import StateManager
    from tests.test_outbox_native import Cfg, StubBrain

    class E(Env):
        instantly_api_key = ""

    state = StateManager(os.path.join(tmp_path, "t.db"))
    await state.init_db()
    handler = Handler(brain=StubBrain(), state=state, config=Cfg(), env=E())
    handler.provider = provider(graph)
    graph.fail[("GET", "/inbox/messages")] = 500

    assert await handler.run() is False


def test_registry_builds_the_microsoft_provider():
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
    from tests.test_outbox_native import Cfg

    class MsCfg(Cfg):
        class channels(Cfg.channels):
            class email(Cfg.channels.email):
                provider = "microsoft"

    assert "microsoft" in NATIVE_PROVIDERS
    assert isinstance(get_mail_provider(MsCfg(), Env()), MicrosoftProvider)


@pytest.mark.asyncio
async def test_wrong_account_is_refused(graph):
    class C:
        class persona:
            email = "you@sender.example"

    p = MicrosoftProvider(config=C(), env=Env(), transport=httpx.MockTransport(graph))
    # The fake Graph says the token belongs to owner@sender.example
    result = await p.send_email("jane@acme.com", "booking", "Hi.")
    assert not result.ok and "you@sender.example" in result.error
    assert ("POST", "/v1.0/me/messages") not in graph.calls()
    with pytest.raises(GraphError, match="signed in as owner@sender.example"):
        await p.get_replies()
    ok, detail = await p.test_connection()
    assert not ok and "persona.email" in detail


def test_pkce_challenge_matches_verifier():
    import base64
    import hashlib

    verifier, challenge = microsoft._pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.decode().rstrip("=")


# ── Pagination and malformed responses: never a partial inbox ──


@pytest.mark.asyncio
async def test_every_page_of_the_week_is_read(graph):
    graph.inbox = [message(i) for i in range(120)]
    replies = await provider(graph).get_replies()

    assert [m.provider_id for m in replies] == [f"m{i}" for i in range(120)]
    first, *later = graph.inbox_requests()
    assert len(later) == 2
    assert first.url.params["$top"] == "50"
    for request in later:
        # The nextLink is followed exactly, with no initial params re-added,
        # and still gets the same Prefer header.
        assert "$filter" not in request.url.params and "$top" not in request.url.params
        assert request.headers["Prefer"] == 'outlook.body-content-type="text"'
    assert str(later[0].url) == (
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
        "?%24skip=50&t=50&%24skiptoken=opaque1"
    )


@pytest.mark.asyncio
async def test_limit_is_only_the_page_size(graph):
    graph.inbox = [message(i) for i in range(7)]
    replies = await provider(graph).get_replies(limit=3)

    assert len(replies) == 7
    assert len(graph.inbox_requests()) == 3


@pytest.mark.asyncio
async def test_empty_inbox_is_an_empty_list(graph):
    assert await provider(graph).get_replies() == []
    assert len(graph.inbox_requests()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>not json</html>"),
    httpx.Response(200, content=b'{"value": ['),
    httpx.Response(200, json=[{"id": "m1"}]),
    httpx.Response(200, json="value"),
    httpx.Response(200, json={}),
    httpx.Response(200, json={"value": None}),
    httpx.Response(200, json={"value": {"id": "m1"}}),
    httpx.Response(200),
], ids=["html", "truncated", "list", "string", "no-value", "null-value",
        "object-value", "empty-200"])
async def test_malformed_first_page_raises(graph, response):
    graph.pages[1] = response
    with pytest.raises(GraphError):
        await provider(graph).get_replies()


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    httpx.Response(500, json={"error": {"code": "boom"}}),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json=["m51"]),
    {"value": None},
    {"value": [message(50), "not a message"]},
    {"value": [{**message(50), "id": None}]},
    {"value": [{**message(50), "from": "jane@acme.com"}]},
    {"value": [{**message(50), "from": {"emailAddress": {"address": 7}}}]},
    {"value": [{**message(50), "body": "text"}]},
    {"value": [{**message(50), "internetMessageHeaders": {"In-Reply-To": "x"}}]},
    {"value": [{**message(50), "internetMessageHeaders": [{"name": 1}]}]},
    {"@odata.nextLink": 42},
    {"@odata.nextLink": ""},
], ids=["http-500", "not-json", "list", "null-value", "non-object-message",
        "no-id", "from-string", "address-number", "body-string", "headers-object",
        "header-name-number", "nextlink-number", "nextlink-empty"])
async def test_a_bad_later_page_fails_the_whole_read(graph, override):
    graph.inbox = [message(i) for i in range(120)]
    graph.pages[2] = override
    with pytest.raises(GraphError):
        await provider(graph).get_replies()


@pytest.mark.asyncio
@pytest.mark.parametrize("link", [
    "http://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24skip=50",
    "https://evil.example/v1.0/me/mailFolders/inbox/messages?%24skip=50",
    "https://graph.microsoft.com.evil.example/v1.0/me/mailFolders/inbox/messages?%24skip=50",
    "https://graph.microsoft.com@evil.example/v1.0/me/mailFolders/inbox/messages?%24skip=50",
    "https://user:pass@graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24skip=50",
    "https://graph.microsoft.com:8443/v1.0/me/mailFolders/inbox/messages?%24skip=50",
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24skip=50#frag",
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24skip=50#",
    "https://graph.microsoft.com\\@evil.example/v1.0/me/mailFolders/inbox/messages?x=1",
    "https://graph.microsoft.com/v1.0/me/messages?%24skip=50",
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/m1/send?x=1",
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/../../../sendMail?x=1",
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox%2Fmessages?x=1",
    "https://graph.microsoft.com/beta/me/mailFolders/inbox/messages?%24skip=50",
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
    "/v1.0/me/mailFolders/inbox/messages?%24skip=50",
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?a=1\r\nX-Evil: 1",
], ids=["http", "off-origin", "suffix-host", "userinfo-host", "credentials", "port",
        "fragment", "empty-fragment", "backslash", "other-path", "send-path",
        "dot-segments", "encoded-slash", "beta", "no-query", "relative", "crlf"])
async def test_hostile_next_link_is_refused_before_any_request(graph, link):
    graph.inbox = [message(i) for i in range(60)]
    graph.pages[1] = {"@odata.nextLink": link}
    with pytest.raises(GraphError, match="nextLink"):
        await provider(graph).get_replies()

    # Only the first page went out; nothing (and no token) followed the link.
    assert len(graph.inbox_requests()) == 1
    assert all(r.url.host in ("graph.microsoft.com", "login.microsoftonline.com")
               for r in graph.requests)
    graph_requests = [r for r in graph.requests if r.url.host == "graph.microsoft.com"]
    assert [r.url.path for r in graph_requests] == [
        "/v1.0/me", "/v1.0/me/mailFolders/inbox/messages"]


@pytest.mark.asyncio
async def test_next_link_loop_is_refused(graph):
    graph.inbox = [message(i) for i in range(120)]
    loop = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?%24skip=50&t=50"
    graph.pages[1] = {"@odata.nextLink": loop}
    graph.pages[2] = {"@odata.nextLink": loop}
    with pytest.raises(GraphError, match="repeats"):
        await provider(graph).get_replies()
    assert len(graph.inbox_requests()) == 2


@pytest.mark.asyncio
async def test_next_link_back_to_the_first_page_is_refused(graph):
    def self_link(request):
        if request.url.path.endswith("/inbox/messages"):
            graph.requests.append(request)
            return httpx.Response(200, json={
                "value": [message(0)], "@odata.nextLink": str(request.url)})
        return graph(request)

    p = MicrosoftProvider(config=None, env=Env(), transport=httpx.MockTransport(self_link))
    with pytest.raises(GraphError, match="repeats"):
        await p.get_replies()
    assert len(graph.inbox_requests()) == 1


@pytest.mark.asyncio
async def test_too_many_pages_fails_closed(graph, monkeypatch):
    monkeypatch.setattr(microsoft, "MAX_INBOX_PAGES", 3)
    graph.inbox = [message(i) for i in range(10)]
    with pytest.raises(GraphError, match="more than 3 pages"):
        await provider(graph).get_replies(limit=3)
    assert len(graph.inbox_requests()) == 3

    graph.requests.clear()
    graph.inbox = graph.inbox[:9]  # exactly 3 pages is fine
    assert len(await provider(graph).get_replies(limit=3)) == 9


@pytest.mark.asyncio
async def test_message_repeated_across_pages_is_returned_once(graph):
    graph.inbox = [message(i) for i in range(60)]
    graph.inbox[50] = message(49)  # shifted by new mail arriving mid-read
    replies = await provider(graph).get_replies()
    assert [m.provider_id for m in replies].count("m49") == 1
    assert len(replies) == 59


@pytest.mark.asyncio
async def test_message_without_a_sender_is_skipped(graph):
    graph.inbox = [{**message(0), "from": None}, message(1)]
    assert [m.provider_id for m in await provider(graph).get_replies()] == ["m1"]


# ── Empty responses: fine for send/delete, not for anything with a body ──


@pytest.mark.asyncio
async def test_empty_202_and_204_are_success(graph):
    p = provider(graph)
    assert await p._request("POST", "/me/messages/draft1/send") == {}
    assert await p._request("DELETE", "/me/messages/draft1") == {}


@pytest.mark.asyncio
async def test_malformed_draft_response_fails_the_send(graph):
    def bad_draft(request):
        if request.method == "POST" and request.url.path == "/v1.0/me/messages":
            graph.requests.append(request)
            return httpx.Response(201, content=b"<html>oops</html>")
        return graph(request)

    p = MicrosoftProvider(config=None, env=Env(), transport=httpx.MockTransport(bad_draft))
    result = await p.send_email("jane@acme.com", "booking", "Hi.")
    assert not result.ok and "malformed JSON" in result.error
    assert not any(path.endswith("/send") for _, path in graph.calls())


@pytest.mark.asyncio
async def test_malformed_identity_response_raises(graph):
    def not_json(request):
        if request.url.path == "/v1.0/me":
            return httpx.Response(200, content=b"nope")
        return graph(request)

    p = MicrosoftProvider(config=None, env=Env(), transport=httpx.MockTransport(not_json))
    with pytest.raises(GraphError, match="malformed JSON"):
        await p.get_replies()


# ── With the handler: later pages count, and a bad later page holds sends ──


async def _handler_with_prospect(tmp_path, graph):
    import os
    from harvey.state import StateManager
    from tests.test_outbox_native import _now_iso, make_handler, seed_prospect

    state = StateManager(os.path.join(tmp_path, "t.db"))
    await state.init_db()
    pid = await seed_prospect(state, email="jane@acme.com", status="contacted")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2", body="b",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=2,
    )
    return state, pid, make_handler(state, provider(graph))


@pytest.mark.asyncio
async def test_unsubscribe_on_a_later_page_is_honored(graph, tmp_path):
    graph.inbox = [message(i) for i in range(60)]
    graph.inbox[55] = message(55, sender="Jane@Acme.com", body="Please unsubscribe me.")
    state, pid, handler = await _handler_with_prospect(tmp_path, graph)

    assert await handler.run() is True
    assert len(graph.inbox_requests()) == 2
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert len(await state.get_outbox(status="cancelled")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    httpx.Response(503, headers={"Retry-After": "60"}),
    httpx.Response(200, content=b"not json"),
    {"value": "nope"},
], ids=["outage", "not-json", "bad-value"])
async def test_bad_later_page_holds_sends_and_handles_nothing(graph, tmp_path, override):
    graph.inbox = [message(i) for i in range(60)]
    graph.inbox[3] = message(3, sender="jane@acme.com", body="Please unsubscribe me.")
    graph.pages[2] = override
    state, pid, handler = await _handler_with_prospect(tmp_path, graph)

    assert await handler.run() is False
    # Page 1 was readable, but nothing from a partial inbox is acted on.
    assert not await state.is_reply_processed("m3")
    assert (await state.get_prospect(pid)).status == "contacted"
    assert len(await state.get_outbox(status="approved")) == 1
