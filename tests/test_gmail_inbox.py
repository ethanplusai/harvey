"""A partial Gmail read must not authorize the heartbeat's send phase."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sellbird.integrations.gmail import GmailProvider


def message(message_id, sender='person@example.test', body='Hello'):
    return {'id': message_id, 'payload': {
        'mimeType': 'text/plain',
        'headers': [{'name': 'From', 'value': sender}],
        'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()},
    }}


def provider(responses):
    p = GmailProvider(SimpleNamespace(), SimpleNamespace())
    p._token = AsyncMock(return_value='synthetic-token')   # never the real sign-in
    # These tests are about the complete 7-day read: no history marker, so
    # every get_replies is that read (tests/test_gmail_history.py covers the rest).
    p._current_history_id = AsyncMock(return_value=None)
    p._request = AsyncMock(side_effect=responses)
    return p


@pytest.mark.asyncio
async def test_empty_successful_inbox():
    assert await provider([{'resultSizeEstimate': 0}]).get_replies() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('responses', [
    [None],
    [{'messages': [{'id': 'm1'}]}, None],
    [{'messages': [{'id': 'm1'}], 'nextPageToken': 'next'}, message('m1'), None],
])
async def test_incomplete_read_raises(responses):
    with pytest.raises(RuntimeError):
        await provider(responses).get_replies()


@pytest.mark.asyncio
async def test_paginates_without_changing_query_or_page_size():
    p = provider([
        {'messages': [{'id': 'm1'}], 'nextPageToken': 'opaque-token'}, message('m1'),
        {'messages': [{'id': 'm2'}]}, message('m2'),
    ])
    assert [m.provider_id for m in await p.get_replies(limit=1)] == ['m1', 'm2']
    assert p._request.call_args_list[2].kwargs['params'] == {
        'q': 'in:inbox -from:me newer_than:7d', 'maxResults': 1,
        'pageToken': 'opaque-token',
    }


@pytest.mark.asyncio
async def test_heartbeat_does_not_send_until_gmail_recovers(monkeypatch, tmp_path):
    # The shared harness routes Claude to a canned local Python response,
    # not a model/CLI service. Its mail send only appends to an in-memory list.
    from tests.test_codex_e2e import Harness, _approved_item
    sellbird = Harness(monkeypatch, tmp_path, provider='claude', model='')
    await sellbird.state.init_db()
    await _approved_item(sellbird.state)
    gmail = provider([None, {'resultSizeEstimate': 0}])
    sellbird.mail.get_replies = gmail.get_replies
    seen = {}
    def after_failure():
        seen['sent'] = list(sellbird.mail.sent)
    await sellbird.run_cycles(2, between={1: after_failure})
    assert seen['sent'] == []
    assert [m['to'] for m in sellbird.mail.sent] == ['bob@beta.com']


@pytest.mark.asyncio
@pytest.mark.parametrize('listing', [
    {'messages': None}, {'messages': {}}, {'messages': [None]},
    {'messages': [{'id': ''}]}, {'nextPageToken': ''}, {'nextPageToken': 42},
])
async def test_malformed_page_is_not_an_empty_inbox(listing):
    with pytest.raises(RuntimeError):
        await provider([listing]).get_replies()


@pytest.mark.asyncio
async def test_repeated_page_token_fails_closed():
    p = provider([{'nextPageToken': 'same'}, {'nextPageToken': 'same'}])
    with pytest.raises(RuntimeError, match='repeated'):
        await p.get_replies()
    assert p._request.await_count == 2


@pytest.mark.asyncio
async def test_page_bound_does_not_return_a_partial_inbox(monkeypatch):
    import sellbird.integrations.gmail as gmail
    monkeypatch.setattr(gmail, 'MAX_INBOX_PAGES', 2, raising=False)
    with pytest.raises(RuntimeError, match='partial'):
        await provider([{'nextPageToken': 'one'}, {'nextPageToken': 'two'}]).get_replies()


@pytest.mark.asyncio
async def test_message_repeated_across_pages_is_read_once():
    p = provider([
        {'messages': [{'id': 'm1'}], 'nextPageToken': 'next'}, message('m1'),
        {'messages': [{'id': 'm1'}, {'id': 'm2'}]}, message('m2'),
    ])
    assert [m.provider_id for m in await p.get_replies()] == ['m1', 'm2']
    assert p._request.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(503, '{}'), (200, 'not json'), (200, 'null')])
async def test_http_failures_do_not_become_an_empty_inbox(monkeypatch, status, body):
    import httpx
    import sellbird.integrations.gmail as gmail
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    monkeypatch.setattr(gmail.httpx, 'AsyncClient',
                        lambda **kwargs: client(transport=transport, **kwargs))
    p = GmailProvider(SimpleNamespace(), SimpleNamespace())
    p._token = AsyncMock(return_value='synthetic-token')
    with pytest.raises(RuntimeError):
        await p.get_replies()


@pytest.mark.asyncio
async def test_second_page_opt_out_cancels_approved_send(monkeypatch, tmp_path):
    from tests.test_codex_e2e import Harness, _approved_item
    sellbird = Harness(monkeypatch, tmp_path, provider='claude', model='')
    await sellbird.state.init_db()
    await _approved_item(sellbird.state)
    gmail = provider([
        {'messages': [{'id': 'old'}], 'nextPageToken': 'next'}, message('old'),
        {'messages': [{'id': 'stop'}]}, message('stop', 'bob@beta.com', 'unsubscribe'),
    ])
    # Already handled newest mail must not hide an older opt-out forever.
    await sellbird.state.mark_reply_processed('old')
    sellbird.mail.get_replies = gmail.get_replies
    await sellbird.run_cycles(1)
    assert sellbird.mail.sent == []
    assert await sellbird.state.is_reply_processed('stop')
    assert len(await sellbird.state.get_outbox(status='cancelled')) == 1


@pytest.mark.asyncio
async def test_a_message_already_read_is_not_downloaded_again():
    """Every cycle reads the whole week's inbox. A Gmail message never
    changes, so only new ones are fetched; the listing still comes first."""
    p = provider([
        {'messages': [{'id': 'm1'}, {'id': 'm2'}]}, message('m1'), message('m2', body='Stop'),
        {'messages': [{'id': 'm3'}, {'id': 'm1'}, {'id': 'm2'}]}, message('m3'),
        {'messages': [{'id': 'm3'}]},
    ])
    first = await p.get_replies()
    second = await p.get_replies()
    assert [m.provider_id for m in first] == ['m1', 'm2']
    assert [m.provider_id for m in second] == ['m3', 'm1', 'm2']
    assert second[2].body == 'Stop'
    assert [m.provider_id for m in await p.get_replies()] == ['m3']
    fetched = [c.args[1] for c in p._request.call_args_list if c.args[1] != '/messages']
    assert fetched == ['/messages/m1', '/messages/m2', '/messages/m3']
    assert set(p._read_cache) == {'m3'}          # what left the week is forgotten


@pytest.mark.asyncio
async def test_a_failed_read_keeps_what_it_did_read():
    p = provider([{'messages': [{'id': 'm1'}], 'nextPageToken': 'next'}, message('m1'), None])
    with pytest.raises(RuntimeError):
        await p.get_replies()
    assert p._read_cache.get('m1') is not None                 # read fine: kept
    p._request = AsyncMock(side_effect=[{'messages': [{'id': 'm1'}]}])
    assert [m.provider_id for m in await p.get_replies()] == ['m1']


@pytest.mark.asyncio
async def test_pages_are_as_big_as_gmail_allows_by_default():
    """40 pages of 50 stopped sending for good past 2,000 messages a week;
    a listing is ids only, so pages of 500 cost nothing extra."""
    p = provider([{'resultSizeEstimate': 0}])
    await p.get_replies()
    assert p._request.call_args.kwargs['params']['maxResults'] == 500


@pytest.mark.asyncio
@pytest.mark.parametrize('expires_in', ['3600', 'soon', None])
async def test_a_garbled_token_answer_is_no_token_not_a_crash(monkeypatch, expires_in):
    """It raised out of the send, so the sender recorded 'maybe sent' for an
    email that never left."""
    import httpx
    import sellbird.integrations.gmail as gmail
    body = 'not json' if expires_in == '3600' else {'access_token': 't', 'expires_in': expires_in}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, **(
        {'text': body} if isinstance(body, str) else {'json': body})))
    client = httpx.AsyncClient
    monkeypatch.setattr(gmail.httpx, 'AsyncClient', lambda **kw: client(transport=transport, **kw))
    monkeypatch.setattr(gmail, '_load_token', lambda: {'refresh_token': 'r'})
    p = GmailProvider(SimpleNamespace(), SimpleNamespace(gmail_client_id='i', gmail_client_secret='s'))
    token = await p._token()
    assert token in (None, 't')
    if expires_in is None:
        assert token == 't'
