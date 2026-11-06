"""Gmail reads only what changed since the last check.

The 7-day read (#62/#69) is complete but grows with the mailbox. Gmail's
history list says what was added since a marker, so a big inbox costs the
same as a small one. The marker only moves once every reply read has been
handled (confirm_read), a marker Gmail no longer has (404) falls back to the
7-day read, and a full read still runs once a day as a backstop.
"""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sellbird.integrations import gmail as gmail_mod
from sellbird.integrations.gmail import GmailError, GmailProvider


def message(mid, sender="person@example.test", body="Hello", labels=("INBOX",)):
    return {"id": mid, "labelIds": list(labels), "payload": {
        "mimeType": "text/plain",
        "headers": [{"name": "From", "value": sender}],
        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
    }}


class FakeGmail:
    """Answers _request by path. ``inbox`` is the 7-day listing; ``history``
    maps a start id to its pages (or 404)."""

    def __init__(self):
        self.profile_history = "100"
        self.inbox = []
        self.history = {}
        self.messages = {}
        self.calls = []

    async def __call__(self, method, path, **kw):
        params = kw.get("params") or {}
        self.calls.append((path, dict(params)))
        if path == "/profile":
            return {"historyId": self.profile_history}
        if path == "/messages":
            return {"messages": [{"id": m} for m in self.inbox]} if self.inbox else {}
        if path == "/history":
            pages = self.history.get(params["startHistoryId"])
            if pages == 404:
                return kw.get("not_found")
            return pages[int(params.get("pageToken") or 0)]
        mid = path.rsplit("/", 1)[1]
        return self.messages.get(mid)

    def paths(self):
        return [p for p, _ in self.calls]


def provider(fake):
    p = GmailProvider(SimpleNamespace(), SimpleNamespace())
    p._token = AsyncMock(return_value="t")
    p._request = fake
    return p


def added(*ids, key="messagesAdded", labels=None):
    if key == "labelsAdded":
        return {key: [{"message": {"id": i}, "labelIds": list(labels or ["INBOX"])} for i in ids]}
    return {key: [{"message": {"id": i}} for i in ids]}


@pytest.mark.asyncio
async def test_first_read_is_full_then_only_what_changed():
    fake = FakeGmail()
    fake.inbox = ["m1"]
    fake.messages = {"m1": message("m1"), "m2": message("m2", body="unsubscribe")}
    p = provider(fake)
    assert [m.provider_id for m in await p.get_replies()] == ["m1"]
    assert fake.paths()[:2] == ["/profile", "/messages"]      # marker before the listing
    p.confirm_read()

    fake.calls.clear()
    fake.history = {"100": [{"history": [added("m2")], "historyId": "105"}]}
    # m1 again from the week's re-listing: the handler skips it as processed.
    assert [m.provider_id for m in await p.get_replies()] == ["m2", "m1"]
    # The first history read after a full one lists the week once more
    # (search-index lag); after that, history alone.
    assert fake.paths() == ["/history", "/messages/m2", "/messages"]
    assert fake.calls[0][1]["labelId"] == "INBOX"
    p.confirm_read()

    fake.calls.clear()
    fake.history["105"] = [{"historyId": "105"}]              # nothing new
    assert await p.get_replies() == []
    assert fake.paths() == ["/history"]


@pytest.mark.asyncio
async def test_an_unconfirmed_read_is_read_again():
    """A reply that couldn't be handled (the model was down) comes back."""
    fake = FakeGmail()
    fake.messages = {"m2": message("m2", body="stop")}
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()                                          # marker 100
    fake.history = {"100": [{"history": [added("m2")], "historyId": "105"}]}
    assert [m.provider_id for m in await p.get_replies()] == ["m2"]
    # not confirmed: the handler stopped part-way
    assert [m.provider_id for m in await p.get_replies()] == ["m2"]
    assert [c for c in fake.calls if c[0] == "/history"][-1][1]["startHistoryId"] == "100"


@pytest.mark.asyncio
async def test_a_marker_gmail_no_longer_has_falls_back_to_the_week():
    fake = FakeGmail()
    fake.inbox = ["m1"]
    fake.messages = {"m1": message("m1")}
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()
    fake.history = {"100": 404}
    fake.profile_history = "300"
    fake.calls.clear()
    assert [m.provider_id for m in await p.get_replies()] == ["m1"]
    assert fake.paths()[:3] == ["/history", "/profile", "/messages"]
    p.confirm_read()
    assert p._history_id == "300"


@pytest.mark.asyncio
async def test_a_full_read_runs_daily_anyway(monkeypatch):
    fake = FakeGmail()
    p = provider(fake)
    clock = [1_000_000.0]
    monkeypatch.setattr(gmail_mod.time, "time", lambda: clock[0])
    await p.get_replies()
    p.confirm_read()
    clock[0] += 25 * 3600
    fake.calls.clear()
    await p.get_replies()
    assert "/history" not in fake.paths() and "/messages" in fake.paths()


@pytest.mark.asyncio
async def test_what_counts_as_an_inbox_reply():
    fake = FakeGmail()
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()
    fake.messages = {
        "mine": message("mine", labels=("SENT",)),                    # Sellbird's own email
        "draft": message("draft", labels=("DRAFT",)),
        "skipped": message("skipped", labels=("CATEGORY_UPDATES",)),  # never reached the inbox
        "moved": message("moved", labels=("CATEGORY_UPDATES",)),     # later moved in
        "reply": message("reply"),
    }
    fake.history = {"100": [{"history": [
        added("mine", "draft", "skipped", "reply"),
        added("moved", key="labelsAdded", labels=["INBOX"]),
        added("reply", key="labelsAdded", labels=["STARRED"]),
    ], "historyId": "110"}]}
    assert [m.provider_id for m in await p.get_replies()] == ["reply", "moved"]


@pytest.mark.asyncio
async def test_history_pages_are_followed_and_bounded():
    fake = FakeGmail()
    fake.messages = {"a": message("a"), "b": message("b")}
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()
    fake.history = {"100": [
        {"history": [added("a")], "nextPageToken": "1", "historyId": "101"},
        {"history": [added("b")], "historyId": "102"},
    ]}
    assert [m.provider_id for m in await p.get_replies()] == ["a", "b"]
    p.confirm_read()
    # The first page's marker: a change made while paging is read again
    # next time rather than skipped (review of #66 follow-up).
    assert p._history_id == "101"
    fake.history["101"] = [{"history": [], "nextPageToken": "0", "historyId": "101"}]
    with pytest.raises(GmailError):                           # a token that repeats
        await p.get_replies()


@pytest.mark.asyncio
async def test_a_profile_that_cant_be_read_still_reads_the_week():
    fake = FakeGmail()
    fake.inbox = ["m1"]
    fake.messages = {"m1": message("m1")}
    fake.profile_history = None
    p = provider(fake)
    assert [m.provider_id for m in await p.get_replies()] == ["m1"]
    p.confirm_read()
    assert p._history_id is None                              # next cycle: the week again


@pytest.mark.asyncio
async def test_the_handler_confirms_only_a_complete_read(tmp_path):
    from sellbird.state import StateManager
    from tests.test_outbox_native import FakeProvider, make_handler
    state = StateManager(str(tmp_path / "c.db"))
    await state.init_db()
    provider_ = FakeProvider()
    provider_.confirm_read = lambda: confirmed.append(True)
    confirmed = []
    handler = make_handler(state, provider_)
    assert await handler._run_native() is True and confirmed == [True]
    provider_.get_replies = AsyncMock(side_effect=RuntimeError("down"))
    assert await handler._run_native() is False and confirmed == [True]


@pytest.mark.asyncio
async def test_a_message_deleted_before_it_was_read_doesnt_stall_sending():
    """It would be listed in every history read and fail every time."""
    fake = FakeGmail()
    fake.messages = {"kept": message("kept")}                 # "gone" returns 404
    original = fake.__call__

    async def with_404(method, path, **kw):
        if path == "/messages/gone":
            fake.calls.append((path, {}))
            return kw.get("not_found")
        return await original(method, path, **kw)
    p = provider(fake)
    p._request = with_404
    await p.get_replies()
    p.confirm_read()
    fake.history = {"100": [{"history": [added("gone", "kept")], "historyId": "101"}]}
    assert [m.provider_id for m in await p.get_replies()] == ["kept"]


@pytest.mark.asyncio
async def test_an_unsubscribe_seen_only_in_history_stops_the_follow_up(monkeypatch, tmp_path):
    """The heartbeat, end to end: cycle 1 reads the week and confirms; cycle 2
    reads just the history, which carries an unsubscribe, and nothing sends."""
    import httpx
    from tests.test_codex_e2e import Harness, _approved_item
    from tests.test_gmail_send_outcomes import transport_provider
    harness = Harness(monkeypatch, tmp_path, provider="claude", model="")
    await harness.state.init_db()
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET", "the opt-out must be handled before any send"
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json={"historyId": "1"})
        if path.endswith("/history"):
            assert request.url.params["startHistoryId"] == "1"
            return httpx.Response(200, json={"history": [added("stop")], "historyId": "2"})
        if path.endswith("/messages/stop"):
            return httpx.Response(200, json=message("stop", "bob@beta.com", "unsubscribe"))
        return httpx.Response(200, json={})                  # cycle 1: an empty week
    p = transport_provider(monkeypatch, respond)
    harness.mail.get_replies = p.get_replies
    harness.mail.confirm_read = p.confirm_read
    harness.mail.send_email = p.send_email
    await harness.run_cycles(1)
    assert p._history_id == "1"
    await _approved_item(harness.state)                       # a follow-up to Bob, due now
    calls.clear()
    await harness.run_cycles(1)
    # history, the unsubscribe, and the week once more (search-index lag)
    assert [r.url.path.rsplit("/", 1)[1] for r in calls] == ["history", "stop", "messages"]
    assert await harness.state.is_reply_processed("stop")
    assert len(await harness.state.get_outbox(status="cancelled")) == 1
    assert p._history_id == "2"


# ── review: ways a reply could be missed ──


@pytest.mark.asyncio
async def test_a_reply_the_search_index_hadnt_caught_up_with_is_still_read():
    """The full read takes the marker, then lists through Gmail search,
    which can lag new mail by seconds. A reply below the marker that the
    listing missed would be in neither read, so the first history read
    after a full read lists the week again too."""
    fake = FakeGmail()
    fake.messages = {"late": message("late", body="unsubscribe")}
    p = provider(fake)
    assert await p.get_replies() == []                        # index hasn't caught up
    p.confirm_read()
    fake.inbox = ["late"]                                     # it has now
    fake.history = {"100": [{"historyId": "100"}]}            # nothing after the marker
    assert [m.provider_id for m in await p.get_replies()] == ["late"]
    p.confirm_read()
    fake.calls.clear()
    fake.history["100"] = [{"historyId": "100"}]
    await p.get_replies()
    assert fake.paths() == ["/history"]                       # then history alone


@pytest.mark.asyncio
async def test_a_reply_moved_back_into_the_inbox_is_judged_on_its_labels_now():
    fake = FakeGmail()
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()
    fake.messages = {"r": message("r", body="stop", labels=("TRASH",))}
    fake.history = {"100": [{"history": [added("r")], "historyId": "101"}]}
    assert await p.get_replies() == []                        # trashed before Sellbird looked
    p.confirm_read()
    fake.messages["r"] = message("r", body="stop", labels=("INBOX",))   # moved back
    fake.history["101"] = [{"history": [added("r", key="labelsAdded", labels=["INBOX"])],
                            "historyId": "102"}]
    assert [m.provider_id for m in await p.get_replies()] == ["r"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["brain", "error"])
async def test_the_handler_doesnt_confirm_when_a_reply_wasnt_handled(tmp_path, monkeypatch, failure):
    from sellbird.brain import BrainUnavailable
    from sellbird.integrations.mail_provider import InboundMessage
    from sellbird.state import StateManager
    from tests.test_outbox_native import FakeProvider, make_handler, seed_prospect
    state = StateManager(str(tmp_path / "u.db"))
    await state.init_db()
    await seed_prospect(state, email="jo@acme.com", status="contacted")
    provider_ = FakeProvider()
    provider_.inbound = [InboundMessage(provider_id="x1", from_email="jo@acme.com",
                                        subject="Re: hi", body="Tell me more")]
    confirmed = []
    provider_.confirm_read = lambda: confirmed.append(True)
    handler = make_handler(state, provider_)

    async def boom(*a, **k):
        raise BrainUnavailable("model down") if failure == "brain" else ValueError("bad")
    monkeypatch.setattr(handler, "_process_reply", boom)
    assert await handler._run_native() is False
    assert confirmed == []


@pytest.mark.asyncio
async def test_a_first_history_page_without_a_marker_is_an_error():
    fake = FakeGmail()
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()
    fake.history = {"100": [{"history": [], "nextPageToken": "1"}, {"history": [], "historyId": "999"}]}
    with pytest.raises(GmailError):
        await p.get_replies()


@pytest.mark.asyncio
async def test_a_failed_recheck_keeps_the_marker_and_tries_again():
    fake = FakeGmail()
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()                                          # marker 100, recheck due
    fake.history = {"100": [{"historyId": "100"}]}
    original = fake.__call__

    async def week_down(method, path, **kw):
        if path == "/messages":
            return None
        return await original(method, path, **kw)
    p._request = week_down
    with pytest.raises(GmailError):
        await p.get_replies()
    p.confirm_read()                                          # nothing to confirm
    assert p._history_id == "100" and p._verify_week is True
    p._request = fake
    fake.calls.clear()
    await p.get_replies()
    assert fake.paths() == ["/history", "/messages"]          # the recheck runs again


@pytest.mark.asyncio
async def test_an_expired_marker_during_the_recheck_reads_the_week_and_rechecks_after():
    fake = FakeGmail()
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()
    fake.history = {"100": 404}
    fake.profile_history = "200"
    await p.get_replies()
    p.confirm_read()
    assert p._history_id == "200" and p._verify_week is True


@pytest.mark.asyncio
async def test_the_recheck_keeps_what_history_just_fetched_cached():
    fake = FakeGmail()
    fake.messages = {"old": message("old")}                   # moved in, older than the week
    p = provider(fake)
    await p.get_replies()
    p.confirm_read()
    fake.history = {"100": [{"history": [added("old", key="labelsAdded")], "historyId": "101"}]}
    await p.get_replies()
    assert "old" in p._read_cache
