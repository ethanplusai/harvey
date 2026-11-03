"""TrulyInbox: the warmup service Harvey recommends, and reads.

Harvey only reads from TrulyInbox (is the sending mailbox enrolled, is warmup
on, how much of its warmup mail lands in spam), so `harvey warmup` can say
whether a mailbox is actually warmed instead of counting days from a date.
"""

import json
from datetime import date, timedelta

import httpx
import pytest

from harvey import deliverability as d
from harvey.config import WarmupConfig
from harvey.integrations import trulyinbox as ti

KEY = "ti_ws_test_key_123"
ADDR = "sales@getacme.example"


def _ok(data):
    return httpx.Response(200, json={"success": True, "data": data,
                                     "errorCode": None, "errorMessage": None})


def _vendor(started="2026-10-01T00:00:00Z", warmup="active", spam=1, sent=100,
            accounts=None, calls=None):
    def respond(request: httpx.Request):
        if calls is not None:
            calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert request.method == "GET"
        path = request.url.path
        if path == "/v2/workspaces":
            return _ok([{"id": 42, "name": "Acme"}])
        if path == "/v2/workspaces/42/accounts":
            return _ok(accounts if accounts is not None
                       else [{"id": 7, "emailAddress": ADDR.upper()}])
        if path == "/v2/workspaces/42/accounts/7/warmup":
            return _ok({"warmupStatus": warmup, "accountState": "active",
                        "warmupStartDate": started})
        if path == "/v2/workspaces/42/accounts/7/analytics/stats":
            return _ok({"deliverabilityRate": 97.5,
                        "dailyBreakdown": [{"sent": sent, "spamDetected": spam}]})
        return httpx.Response(404, json={"success": False, "errorCode": "000404"})
    return httpx.MockTransport(respond)


@pytest.fixture(autouse=True)
def _fresh_latch():
    ti._REJECTED.clear()
    yield
    ti._REJECTED.clear()


# ── Reading a mailbox ──

@pytest.mark.asyncio
async def test_reads_the_sending_mailboxs_warmup():
    h = await ti.TrulyInbox(KEY, transport=_vendor()).mailbox_health(ADDR)
    assert h.found and h.warming and h.login_ok
    assert h.started_on == date(2026, 10, 1)
    assert (h.sent, h.spam, h.score) == (100, 1, 97.5)
    assert h.spam_rate == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_a_mailbox_not_in_trulyinbox_is_reported_not_found():
    h = await ti.TrulyInbox(KEY, transport=_vendor(accounts=[])).mailbox_health(ADDR)
    assert not h.found


@pytest.mark.asyncio
async def test_a_set_workspace_id_skips_the_lookup():
    calls = []
    client = ti.TrulyInbox(KEY, workspace_id="42", transport=_vendor(calls=calls))
    await client.mailbox_health(ADDR)
    assert "/v2/workspaces" not in [c.url.path for c in calls]


@pytest.mark.asyncio
async def test_only_reads_never_writes():
    calls = []
    await ti.TrulyInbox(KEY, transport=_vendor(calls=calls)).mailbox_health(ADDR)
    assert calls and {c.method for c in calls} == {"GET"}


# ── Errors ──

def _fail(status, code, message="nope"):
    return httpx.MockTransport(lambda r: httpx.Response(
        status, json={"success": False, "data": None, "errorCode": code, "errorMessage": message}))


@pytest.mark.asyncio
async def test_a_rejected_key_is_never_sent_again():
    """TrulyInbox locks the IP out after five rejected keys."""
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(401, json={"success": False, "errorCode": "150001"})
    for _ in range(3):
        with pytest.raises(ti.TrulyInboxError, match="rejected"):
            await ti.TrulyInbox(KEY, transport=httpx.MockTransport(respond)).mailbox_health(ADDR)
    assert len(calls) == 1
    ti.forget_rejected(KEY)   # connecting a key again clears it
    with pytest.raises(ti.TrulyInboxError):
        await ti.TrulyInbox(KEY, transport=httpx.MockTransport(respond)).mailbox_health(ADDR)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status, code, words", [
    (403, "150003", "scope"),
    (403, "150007", "Starter"),
    (403, "040006", "paused"),
    (429, "150005", "limit"),
    (500, None, "didn't answer"),
])
async def test_errors_say_what_to_do(status, code, words):
    with pytest.raises(ti.TrulyInboxError, match=words):
        await ti.TrulyInbox(KEY, workspace_id="42",
                            transport=_fail(status, code)).mailbox_health(ADDR)


@pytest.mark.asyncio
async def test_the_key_never_appears_in_an_error_or_repr():
    transport = _fail(400, "999999", f"bad key {KEY}")
    client = ti.TrulyInbox(KEY, workspace_id="42", transport=transport)
    with pytest.raises(ti.TrulyInboxError) as exc:
        await client.mailbox_health(ADDR)
    assert KEY not in str(exc.value) and KEY not in repr(client)


@pytest.mark.asyncio
async def test_a_network_failure_is_a_plain_error():
    def boom(request):
        raise httpx.ConnectError("down", request=request)
    with pytest.raises(ti.TrulyInboxError, match="didn't answer"):
        await ti.TrulyInbox(KEY, workspace_id="42",
                            transport=httpx.MockTransport(boom)).mailbox_health(ADDR)


@pytest.mark.asyncio
async def test_odd_shapes_never_crash():
    def respond(request):
        if request.url.path.endswith("/accounts"):
            return _ok([{"id": 7, "emailAddress": ADDR}, "junk", None])
        if request.url.path.endswith("/warmup"):
            return _ok("not a dict")
        return _ok({"deliverabilityRate": "high", "dailyBreakdown": [None, {"sent": True}]})
    h = await ti.TrulyInbox(KEY, workspace_id="42",
                            transport=httpx.MockTransport(respond)).mailbox_health(ADDR)
    assert h.found and not h.warming and h.score is None and h.sent == 0


def test_a_key_that_cant_go_in_a_header_is_refused():
    with pytest.raises(ti.TrulyInboxError):
        ti.TrulyInbox("bad\nkey")


# ── What `harvey warmup` says ──

TODAY = date(2026, 10, 25)


def _health(**kw):
    base = dict(address=ADDR, found=True, warming=True, login_ok=True,
                started_on=TODAY - timedelta(days=24), sent=200, spam=2, score=98.0)
    base.update(kw)
    return ti.MailboxHealth(**base)


def test_not_connected_recommends_trulyinbox_with_the_link():
    c = d.check_warmup_service(None, today=TODAY)
    assert ti.AFFILIATE_URL in c.detail and "harvey trulyinbox connect" in c.detail
    assert c.status == d.INFO


def test_warmed_mailbox_is_ok_and_shows_inbox_rate():
    c = d.check_warmup_service(_health(), WarmupConfig(start_date="2026-10-01"), today=TODAY)
    assert c.status == d.OK and "day 24" in c.detail and "99% inbox" in c.detail


def test_too_few_days_is_not_ready():
    c = d.check_warmup_service(_health(started_on=TODAY - timedelta(days=5)), today=TODAY)
    assert c.status == d.FAIL and "day 5 of 14" in c.detail


def test_too_much_spam_is_not_ready():
    c = d.check_warmup_service(_health(sent=100, spam=8), today=TODAY)
    assert c.status == d.FAIL and "8% spam" in c.detail


@pytest.mark.parametrize("kw, words", [
    ({"found": False}, "isn't in your TrulyInbox"),
    ({"login_ok": False}, "sign in"),
    ({"warming": False}, "isn't on"),
])
def test_problems_in_trulyinbox_say_what_to_fix(kw, words):
    c = d.check_warmup_service(_health(**kw), today=TODAY)
    assert c.status in (d.FAIL, d.WARN) and words in c.detail


def test_a_vendor_error_is_a_warning_not_a_crash():
    c = d.check_warmup_service(None, error="TrulyInbox didn't answer.", today=TODAY)
    assert c.status == d.WARN and "didn't answer" in c.detail


def test_suggests_the_ramp_start_date_from_trulyinbox():
    c = d.check_warmup_service(_health(), WarmupConfig(), today=TODAY)
    assert "start_date: 2026-10-01" in c.detail


def test_another_warmup_service_is_respected():
    c = d.check_warmup_service(None, other_service=True, today=TODAY)
    assert ti.AFFILIATE_URL not in c.detail


# ── The link is defined once ──

def test_the_link_lives_in_one_constant():
    from harvey.paths import PROJECT_ROOT
    src = [p for p in (PROJECT_ROOT / "harvey").rglob("*") if p.suffix in (".py", ".js", ".html")]
    assert [p.name for p in src if "trulyinbox.com/?via" in p.read_text(encoding="utf-8")] == ["trulyinbox.py"]
    for name in ("README.md", "CLAUDE.md", "harvey.yaml"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert ti.AFFILIATE_URL in text, name
        # every link to the site carries the referral
        assert text.count("trulyinbox.com/") - text.count("dev.trulyinbox.com/") \
            == text.count(ti.AFFILIATE_URL), name


# ── Remembering the choice, so nobody is asked twice ──

@pytest.mark.asyncio
async def test_the_choice_is_remembered(tmp_path):
    from harvey.state import StateManager
    state = StateManager(str(tmp_path / "h.db"))
    await state.init_db()
    assert await ti.get_choice(state) == ""
    await ti.set_choice(state, "other")
    assert await ti.get_choice(state) == "other"
    with pytest.raises(ValueError):
        await ti.set_choice(state, "whatever")
    stored = json.loads(await state.get_setting(ti.CHOICE_KEY))
    assert stored["choice"] == "other"


# ── Review follow-ups ──

@pytest.mark.parametrize("sent", [0, 5, 19])
def test_too_little_warmup_mail_is_never_called_warmed(sent):
    c = d.check_warmup_service(_health(sent=sent, spam=0), today=TODAY)
    assert c.status != d.OK and "not enough" in c.detail


@pytest.mark.asyncio
async def test_a_workspace_id_that_isnt_a_number_is_refused():
    def respond(request):
        return _ok([{"id": "42\nSMTP_HOST=evil.example", "name": "x"}])
    with pytest.raises(ti.TrulyInboxError, match="workspace"):
        await ti.TrulyInbox(KEY, transport=httpx.MockTransport(respond)).mailbox_health(ADDR)
    with pytest.raises(ti.TrulyInboxError, match="TRULYINBOX_WORKSPACE_ID"):
        await ti.TrulyInbox(KEY, workspace_id="../x", transport=_vendor()).mailbox_health(ADDR)


@pytest.mark.asyncio
async def test_an_ip_lockout_is_not_called_a_bad_key():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, json={"success": False, "errorCode": "150006"})
    with pytest.raises(ti.TrulyInboxError, match="15 minutes"):
        await ti.TrulyInbox(KEY, "42", transport=httpx.MockTransport(respond)).mailbox_health(ADDR)
    assert ti._hash(KEY) not in ti._REJECTED


@pytest.mark.parametrize("health, error, choice, words", [
    (None, "", "", "not set up"),
    (None, "", "other", "another warmup service"),
    (None, ti.KEY_REJECTED, "connected", "connected, but"),
    ("h", "", "connected", "connected"),
])
def test_the_status_line_is_honest(health, error, choice, words):
    h = _health() if health else None
    assert words in ti.describe_status(h, error, choice)


def test_the_missing_scope_message_names_every_scope_connect_asks_for():
    for scope in ("accounts:read", "warmup:read", "analytics:read", "workspace:read"):
        assert scope in ti.MISSING_SCOPE
