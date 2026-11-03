"""Gmail's send result must preserve the outbox's uncertainty boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import sellbird.dashboard as dashboard
import sellbird.integrations.gmail as gmail
from sellbird.state import MAYBE_SENT_PREFIX, StateManager
from tests.test_outbox_native import make_sender, seed_prospect, _now_iso
from tests.test_gmail_inbox import message


def transport_provider(monkeypatch, handler):
    client = httpx.AsyncClient

    # The patch below replaces httpx.AsyncClient for the whole process, so
    # other requests in a heartbeat (the Claude usage check) come here too.
    # Only Gmail's reach the scripted handler, so they can't use up its replies.
    def gmail_only(request):
        if request.url.host != 'gmail.googleapis.com':
            return httpx.Response(404, json={})
        return handler(request)
    transport = httpx.MockTransport(gmail_only)
    monkeypatch.setattr(gmail.httpx, 'AsyncClient',
                        lambda **kwargs: client(transport=transport, **kwargs))
    config = SimpleNamespace(persona=SimpleNamespace(
        name='Synthetic', email='sender@example.invalid'))
    p = gmail.GmailProvider(config, SimpleNamespace())
    p._token = AsyncMock(return_value='synthetic-token')
    p.is_configured = lambda: True
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,outcome', [
    (200, '{"id":"gmail-id", "threadId":"thread-id"}', 'sent'),
    (400, '{"error":{"message":"invalid request"}}', 'rejected'),
    (401, 'unauthorized', 'rejected'),
    (403, '{}', 'rejected'),
    (429, '{}', 'rejected'),
    (200, 'not json', 'uncertain'),
    (200, 'null', 'uncertain'),
    (200, '{}', 'uncertain'),
    (200, '{"id":null}', 'uncertain'),
    (200, '{"id":42}', 'uncertain'),
    (200, '{"id":" "}', 'uncertain'),
    (200, '[]', 'uncertain'),
    (204, '', 'uncertain'),
    (302, '{"id":"not-a-success"}', 'uncertain'),
    (503, '{}', 'uncertain'),
    (0, '', 'uncertain'),
])
async def test_sender_outbox_and_dashboard_outcome(
        monkeypatch, tmp_path, status, body, outcome):
    posts = []
    def respond(request):
        if request.method == 'POST':
            posts.append(request)
            if status == 0:
                raise httpx.ReadTimeout('synthetic timeout', request=request)
            return httpx.Response(status, text=body)
        assert request.method == 'GET'
        return httpx.Response(200, json={'payload': {'headers': [
            {'name': 'Message-ID', 'value': '<rfc-id@example.invalid>'}]}})
    p = transport_provider(monkeypatch, respond)
    path = tmp_path / 'state.db'
    state = StateManager(str(path))
    await state.init_db()
    pid = await seed_prospect(state, email='recipient@example.invalid')
    iid = await state.add_outbox_item(
        prospect_id=pid, to_email='recipient@example.invalid', subject='Synthetic',
        body='Hello, would this help your team?', send_at=_now_iso(), status='approved')
    sender = make_sender(state, p)
    await sender._drain_due()
    item = await state.get_outbox_item(iid)
    assert item['status'] == ('sent' if outcome == 'sent' else 'failed')
    assert item['error'].startswith(MAYBE_SENT_PREFIX) == (outcome == 'uncertain')
    if outcome == 'sent':
        assert item['message_id'] == '<rfc-id@example.invalid>'
        assert item['thread_ref'] == 'thread-id'
    monkeypatch.setattr(dashboard, 'DB_PATH', path)
    assert (pid in await dashboard._sellbird_committed_ids()) == (outcome != 'rejected')
    assert await dashboard._sellbird_sent_to(pid) == (outcome != 'rejected')
    # The provider recovers, but neither a second drain nor a fresh process
    # may silently re-send an already attempted outbox item.
    status, body = 200, '{"id":"recovered"}'
    await sender._drain_due()
    reopened = StateManager(str(path))
    await reopened.init_db()
    await make_sender(reopened, p)._drain_due()
    assert len(posts) == 1
    assert (await reopened.get_outbox_item(iid))['status'] == item['status']


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [
    (503, '{}'), (200, 'not json'), (200, 'null'), (200, '[]'),
    (200, '{"payload": []}'), (200, '{"payload":{"headers":42}}'),
    (200, '{"payload":{"headers":[null,{"name":42},{"name":"Message-ID","value":null}]}}'),
])
async def test_metadata_is_optional_after_confirmed_send(monkeypatch, status, body):
    calls = []
    def respond(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(200, json={'id': 'confirmed', 'threadId': 'thread'})
        return httpx.Response(status, text=body)
    p = transport_provider(monkeypatch, respond)
    result = await p.send_email('recipient@example.invalid', 'Synthetic', 'Hello')
    assert result.ok
    assert result.message_id == 'confirmed'
    assert result.thread_ref == 'thread'
    assert calls == ['POST', 'GET']


@pytest.mark.asyncio
async def test_no_token_does_not_attempt_send(monkeypatch):
    def unexpected(request):
        pytest.fail('No request should be sent without a token')
    p = transport_provider(monkeypatch, unexpected)
    p._token = AsyncMock(return_value=None)
    assert not (await p.send_email('recipient@example.invalid', 'Synthetic', 'Hello')).ok


@pytest.mark.asyncio
async def test_transport_read_failure_then_recovery_sends_once(monkeypatch, tmp_path):
    from tests.test_codex_e2e import Harness, _approved_item
    harness = Harness(monkeypatch, tmp_path, provider='claude', model='')
    await harness.state.init_db()
    await _approved_item(harness.state)
    reads, posts, after_failure = [], [], []
    def respond(request):
        if request.method == 'POST':
            posts.append(request)
            return httpx.Response(200, json={'id': 'sent'})
        if request.url.path.endswith('/messages/sent'):
            return httpx.Response(503, json={})
        if request.url.path.endswith('/profile'):        # the history marker
            return httpx.Response(200, json={'historyId': '1'})
        if request.url.path.endswith('/history'):        # nothing new since it
            return httpx.Response(200, json={'historyId': '1'})
        reads.append(request)
        return (httpx.Response(200, text='malformed') if len(reads) == 1
                else httpx.Response(200, json={}))
    p = transport_provider(monkeypatch, respond)
    harness.mail.get_replies = p.get_replies
    harness.mail.send_email = p.send_email
    await harness.run_cycles(3, between={1: lambda: after_failure.append(len(posts))})
    assert after_failure == [0]
    assert len(posts) == 1
    assert len(await harness.state.get_outbox(status='sent')) == 1


@pytest.mark.asyncio
async def test_transport_second_page_unsubscribe_precedes_send(monkeypatch, tmp_path):
    from tests.test_codex_e2e import Harness, _approved_item
    harness = Harness(monkeypatch, tmp_path, provider='claude', model='')
    await harness.state.init_db()
    await _approved_item(harness.state)
    await harness.state.mark_reply_processed('old')
    calls = []
    def respond(request):
        calls.append(request)
        assert request.method == 'GET', 'Opt-out must be processed before any send'
        path = request.url.path
        if path.endswith('/messages/old'):
            return httpx.Response(200, json=message('old'))
        if path.endswith('/messages/stop'):
            return httpx.Response(200, json=message('stop', 'bob@beta.com', 'unsubscribe'))
        if request.url.params.get('pageToken') == 'next':
            return httpx.Response(200, json={'messages': [{'id': 'stop'}]})
        return httpx.Response(200, json={
            'messages': [{'id': 'old'}], 'nextPageToken': 'next'})
    p = transport_provider(monkeypatch, respond)
    harness.mail.get_replies = p.get_replies
    harness.mail.send_email = p.send_email
    await harness.run_cycles(1)
    assert await harness.state.is_reply_processed('stop')
    assert len(await harness.state.get_outbox(status='cancelled')) == 1
    pages = [r for r in calls if r.url.path.endswith('/messages')]
    assert len(pages) == 2
    assert pages[1].url.params['pageToken'] == 'next'
    assert pages[0].url.params['q'] == pages[1].url.params['q']


@pytest.mark.asyncio
async def test_metadata_exception_cannot_undo_confirmed_send(monkeypatch):
    posts = []
    def respond(request):
        assert request.method == 'POST'
        posts.append(request)
        return httpx.Response(200, json={'id': 'confirmed', 'threadId': None})
    p = transport_provider(monkeypatch, respond)
    # The optional lookup refresh fails after a successful POST response.
    p._token = AsyncMock(side_effect=['synthetic-token', ValueError('bad refresh JSON')])
    result = await p.send_email('recipient@example.invalid', 'Synthetic', 'Hello')
    assert result.ok and result.message_id == 'confirmed'
    assert result.thread_ref == ''
    assert len(posts) == 1
