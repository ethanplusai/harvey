"""InboxWarm.ai: an optional warmup service Harvey reads instead of TrulyInbox."""

import json
from datetime import date, timedelta

import httpx
import pytest

import harvey.dashboard as dash
from harvey import deliverability as d
from harvey.integrations import inboxwarm as iw
from harvey.integrations import trulyinbox as ti
from harvey.integrations import warmup_service as ws
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)

ADDR = "jane@sender.test"
KEY = "iw_secret_key"
TODAY = date(2026, 11, 3)


def _api(listing=None, status=None, report=None, codes=None, seen=None):
    """A fake InboxWarm: canned answers per endpoint, optional error codes."""
    listing = listing if listing is not None else {"items": [
        {"id": "12345", "fromEmail": ADDR, "type": "gmail", "status": "warming",
         "createdAt": "2026-10-10T09:00:00.000Z"}], "total": 1}
    status = status if status is not None else {
        "emailAccountId": "12345", "authStatus": "connected", "warmupStatus": "active",
        "healthScore": 92, "connectionError": None}
    report = report if report is not None else {
        "emailAccountId": "12345", "totalDeliverabilityRate": 97,
        "totalSentCount": 200, "totalInboxCount": 194, "totalSpamInboxCount": 6}
    codes = codes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path.removeprefix("/api/v1")
        if path == "/email-accounts":
            key, body = "list", listing
        elif path.endswith("/status"):
            key, body = "status", status
        else:
            key, body = "report", report
        return httpx.Response(codes.get(key, 200), json=body)

    return httpx.MockTransport(handler)


async def _health(**kw):
    seen = kw.pop("seen", None)
    return await iw.InboxWarm(KEY, transport=_api(seen=seen, **kw), today=TODAY).mailbox_health(ADDR)


# ── reading one mailbox ──

@pytest.mark.asyncio
async def test_a_warming_mailbox_reads_back():
    h = await _health()
    assert h.found and h.warming and h.login_ok
    assert h.started_on == date(2026, 10, 10)
    assert (h.sent, h.spam, h.score) == (200, 6, 97.0)
    assert h.spam_rate == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_requests_carry_the_key_where_inboxwarm_wants_it():
    seen = []
    await _health(seen=seen)
    lst, status, report = seen
    assert lst.method == "GET" and lst.url.params["api_key"] == KEY
    assert lst.url.params["search"] == ADDR
    assert status.url.path == "/api/v1/email-accounts/12345/status"
    assert report.method == "POST" and report.url.path == "/api/v1/deliverability-score/12345"
    body = json.loads(report.content)
    assert body == {"apiKey": KEY, "startDate": str(TODAY - timedelta(days=14)),
                    "endDate": str(TODAY)}
    assert "api_key" not in report.url.params


@pytest.mark.asyncio
async def test_a_mailbox_not_in_inboxwarm_is_not_found():
    h = await _health(listing={"items": [{"id": "9", "fromEmail": "other@sender.test"}]})
    assert not h.found


@pytest.mark.asyncio
@pytest.mark.parametrize("status,list_state", [
    ({"authStatus": "disconnected", "warmupStatus": "active"}, "warming"),
    ({"authStatus": "connected", "warmupStatus": "active", "connectionError": "token revoked"},
     "warming"),
    ({"authStatus": "connected", "warmupStatus": "active"}, "auth-expired"),
])
async def test_a_signed_out_mailbox_is_not_warming(status, list_state):
    listing = {"items": [{"id": "12345", "fromEmail": ADDR, "status": list_state}]}
    h = await _health(listing=listing, status=status)
    assert h.found and not h.login_ok and not h.warming


@pytest.mark.asyncio
async def test_paused_warmup_is_not_warming():
    h = await _health(status={"authStatus": "connected", "warmupStatus": "paused"})
    assert h.login_ok and not h.warming


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message", [
    (401, iw.KEY_REJECTED), (403, iw.KEY_REJECTED),
    (429, iw.RATE_LIMITED), (503, iw.NO_ANSWER),
])
async def test_api_errors_become_plain_messages(code, message):
    with pytest.raises(iw.InboxWarmError) as e:
        await _health(codes={"list": code})
    assert str(e.value) == message


@pytest.mark.asyncio
async def test_an_error_message_never_shows_the_key():
    with pytest.raises(iw.InboxWarmError) as e:
        await _health(codes={"list": 400}, listing={"error_code": "validation_error",
                                                     "message": f"bad key {KEY}"})
    assert KEY not in str(e.value) and "[hidden]" in str(e.value)


@pytest.mark.asyncio
async def test_a_network_failure_is_no_answer():
    def boom(request):
        raise httpx.ConnectError("down")
    with pytest.raises(iw.InboxWarmError) as e:
        await iw.InboxWarm(KEY, transport=httpx.MockTransport(boom)).mailbox_health(ADDR)
    assert str(e.value) == iw.NO_ANSWER


@pytest.mark.asyncio
async def test_an_odd_account_id_never_reaches_a_url():
    with pytest.raises(iw.InboxWarmError):
        await _health(listing={"items": [{"id": "../../x", "fromEmail": ADDR}]})


def test_an_empty_key_is_refused():
    with pytest.raises(iw.InboxWarmError):
        iw.InboxWarm("  ")


# ── which service Harvey reads ──

def test_trulyinbox_wins_when_both_are_set(monkeypatch):
    monkeypatch.delenv("TRULYINBOX_API_KEY", raising=False)
    monkeypatch.setenv("INBOXWARM_API_KEY", "i")
    assert ws.active() == "inboxwarm" and ws.label() == "InboxWarm"
    monkeypatch.setenv("TRULYINBOX_API_KEY", "t")
    assert ws.active() == "trulyinbox" and ws.label() == "TrulyInbox"


def test_no_key_means_no_service(monkeypatch):
    monkeypatch.delenv("TRULYINBOX_API_KEY", raising=False)
    monkeypatch.delenv("INBOXWARM_API_KEY", raising=False)
    assert ws.active() == "" and ws.label() == "TrulyInbox"


def test_nothing_connected_still_recommends_trulyinbox():
    c = d.check_warmup_service(None, None, today=TODAY)
    assert "TrulyInbox" in c.detail and "harvey inboxwarm connect" in c.detail


@pytest.mark.asyncio
async def test_current_health_reads_the_active_service(monkeypatch):
    monkeypatch.delenv("TRULYINBOX_API_KEY", raising=False)
    monkeypatch.setenv("INBOXWARM_API_KEY", "i")

    async def fake(address):
        return ti.MailboxHealth(address=address, found=False), ""
    monkeypatch.setattr(iw, "current_health", fake)
    health, error, label = await ws.current_health(ADDR)
    assert not health.found and error == "" and label == "InboxWarm"


def test_messages_name_inboxwarm():
    h = ti.MailboxHealth(address=ADDR, found=True, warming=False)
    c = d.check_warmup_service(h, today=TODAY, service="InboxWarm")
    assert "Turn it on in InboxWarm" in c.detail and "TrulyInbox" not in c.detail


# ── dashboard: connect InboxWarm from the setup step ──

@pytest.fixture
def _clean_env(monkeypatch):
    for k in ("INBOXWARM_API_KEY", "TRULYINBOX_API_KEY", "TRULYINBOX_WORKSPACE_ID"):
        monkeypatch.delenv(k, raising=False)


def _step(client):
    checks = client.get("/api/setup-status").json()["checks"]
    return next(c for c in checks if c["id"] == "warmup_service")


def test_connecting_checks_the_key_then_saves_it(client, monkeypatch, _clean_env):
    seen = {}

    async def fake_health(self, address):
        seen["key"] = self._key
        return ti.MailboxHealth(address=address, found=False)
    monkeypatch.setattr(iw.InboxWarm, "mailbox_health", fake_health)
    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    r = client.post("/api/inboxwarm/connect", json={"key": "  iw_abc  "}).json()
    assert r["success"] and "isn't in your InboxWarm" in r["message"]
    assert seen["key"] == "iw_abc"
    assert written == {"INBOXWARM_API_KEY": "iw_abc"}
    assert _step(client)["done"]


def test_a_rejected_key_is_not_saved(client, monkeypatch, _clean_env):
    async def reject(self, address):
        raise iw.InboxWarmError(iw.KEY_REJECTED)
    monkeypatch.setattr(iw.InboxWarm, "mailbox_health", reject)
    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    r = client.post("/api/inboxwarm/connect", json={"key": "iw_bad"})
    assert r.status_code == 400 and "rejected" in r.json()["message"]
    assert not written and not _step(client)["done"]


@pytest.mark.parametrize("body", [{}, {"key": ""}, {"key": 5}, {"key": "x" * 600}, []])
def test_a_missing_key_is_refused(client, body, _clean_env):
    assert client.post("/api/inboxwarm/connect", json=body).status_code == 400


def test_the_step_names_the_connected_service(client, monkeypatch, _clean_env):
    monkeypatch.setenv("INBOXWARM_API_KEY", "iw_abc")
    step = _step(client)
    assert step["done"] and "InboxWarm connected" in step["label"]
