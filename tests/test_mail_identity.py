"""Who a mailbox sends as, and where its sign-in lives, for every native provider.

The main mailbox is the config's persona with its token file under data/.
A caller connecting another inbox passes its own identity and token source,
and Gmail and Microsoft take an httpx transport so tests never reach Google.
"""

import base64
import email
import json
from types import SimpleNamespace

import httpx
import pytest

from sellbird.integrations import gmail, microsoft, smtp_mail
from sellbird.integrations.mail_provider import (
    INBOX_NOT_CONNECTED,
    MailIdentity,
    SendResult,
    identity_from_config,
    missing_token_text,
    save_if_signed_in_as,
)


def _config(name="Dana", address="dana@example.com"):
    return SimpleNamespace(persona=SimpleNamespace(name=name, email=address),
                           channels=SimpleNamespace(email=SimpleNamespace(signature=None)))


class _Tokens:
    def __init__(self, data=None):
        self.data = data
        self.saved = []

    def load(self):
        return self.data

    def save(self, data):
        self.saved.append(data)


# ── identity ──

def test_the_main_mailbox_is_the_persona():
    assert identity_from_config(_config()) == MailIdentity("dana@example.com", "Dana")
    assert identity_from_config(SimpleNamespace()) == MailIdentity("", "")


def test_missing_token_text_names_the_right_fix():
    assert missing_token_text(None, "Run 'sellbird gmail auth'.") == "Run 'sellbird gmail auth'."
    assert missing_token_text(_Tokens(), "x") == INBOX_NOT_CONNECTED
    why = _Tokens()
    why.missing_reason = lambda: "the vault is locked"
    assert missing_token_text(why, "x") == "the vault is locked"


# ── signing in as the right account ──

def test_a_sign_in_saves_only_for_the_expected_account(capsys):
    saved = []
    assert save_if_signed_in_as({"t": 1}, "Dana@Example.com", lambda: "dana@example.com",
                                saved.append)
    assert saved == [{"t": 1}]
    assert not save_if_signed_in_as({"t": 2}, "dana@example.com", lambda: "admin@example.com",
                                    saved.append)
    assert saved == [{"t": 1}] and "Signed in as admin@example.com" in capsys.readouterr().out


def test_a_sign_in_whose_account_cant_be_read_saves_nothing(capsys):
    def boom():
        raise httpx.ConnectError("down")
    saved = []
    assert not save_if_signed_in_as({"t": 1}, "dana@example.com", boom, saved.append)
    assert saved == []


def test_the_main_mailbox_saves_without_a_check():
    saved = []
    assert save_if_signed_in_as({"t": 1}, None, lambda: pytest.fail("not asked"), saved.append)
    assert saved == [{"t": 1}]


# ── Gmail ──

def _gmail_transport(sent):
    def handle(request: httpx.Request):
        if str(request.url) == gmail.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
        if request.url.path.endswith("/messages/send"):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "m1", "threadId": "t1"})
        if request.url.path.endswith("/messages/m1"):
            return httpx.Response(200, json={"payload": {"headers": [
                {"name": "Message-ID", "value": "<m1@mail.example>"}]}})
        return httpx.Response(404)
    return httpx.MockTransport(handle)


def _env():
    return SimpleNamespace(gmail_client_id="id", gmail_client_secret="secret")


@pytest.mark.asyncio
async def test_gmail_sends_as_the_identity_it_was_given_with_its_own_tokens():
    sent, tokens = [], _Tokens({"refresh_token": "r"})
    provider = gmail.GmailProvider(_config(), _env(), _gmail_transport(sent),
                                   identity=MailIdentity("sales@example.org", "Sam"),
                                   tokens=tokens)
    assert provider.is_configured()
    result = await provider.send_email("lee@example.net", "Hello", "Hi Lee.")
    assert result.ok and result.message_id == "<m1@mail.example>" and result.thread_ref == "t1"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(sent[0]["raw"]))
    assert msg["From"] == "Sam <sales@example.org>" and msg["To"] == "lee@example.net"


@pytest.mark.asyncio
async def test_gmail_defaults_to_the_persona_and_the_token_file(monkeypatch):
    sent = []
    monkeypatch.setattr(gmail, "_load_token", lambda: {"refresh_token": "r"})
    provider = gmail.GmailProvider(_config(), _env(), _gmail_transport(sent))
    assert (await provider.send_email("lee@example.net", "Hello", "Hi.")).ok
    msg = email.message_from_bytes(base64.urlsafe_b64decode(sent[0]["raw"]))
    assert msg["From"] == "Dana <dana@example.com>"


@pytest.mark.asyncio
async def test_gmail_with_an_empty_token_source_says_how_to_connect_it():
    provider = gmail.GmailProvider(_config(), _env(), _gmail_transport([]), tokens=_Tokens(None))
    assert not provider.is_configured()
    assert await provider._token() is None
    assert provider._token_problem == INBOX_NOT_CONNECTED


# ── SMTP ──

@pytest.mark.asyncio
async def test_smtp_sends_as_the_identity_through_its_own_server(monkeypatch):
    seen = {}

    async def fake_send(msg, **kw):
        seen["msg"], seen["kw"] = msg, kw

    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", fake_send)
    provider = smtp_mail.SmtpImapProvider(
        _config(), SimpleNamespace(), identity=MailIdentity("sales@example.org", "Sam"),
        smtp=smtp_mail.SmtpSettings(host="smtp.example.org", username="sales@example.org",
                                    password="pw", port=465))
    assert provider.is_configured()
    result = await provider.send_email("lee@example.net", "Hello", "Hi.")
    assert isinstance(result, SendResult) and result.ok
    assert seen["msg"]["From"] == "Sam <sales@example.org>"
    assert seen["kw"]["hostname"] == "smtp.example.org" and seen["kw"]["use_tls"] is True


def test_smtp_settings_never_show_the_password():
    public = smtp_mail.SmtpSettings(host="h", username="u", password="pw").public()
    assert public["host"] == "h" and "pw" not in json.dumps(public)


# ── Microsoft ──

@pytest.mark.asyncio
async def test_microsoft_with_its_own_tokens_checks_the_signed_in_account():
    def handle(request: httpx.Request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "a", "expires_in": 3600,
                                             "refresh_token": "r2"})
        if request.url.path == "/v1.0/me":
            return httpx.Response(200, json={"mail": "admin@example.org"})
        return httpx.Response(404)

    tokens = _Tokens({"refresh_token": "r"})
    provider = microsoft.MicrosoftProvider(
        _config(), SimpleNamespace(m365_client_id="id"), httpx.MockTransport(handle),
        identity=MailIdentity("sales@example.org", "Sam"), tokens=tokens)
    result = await provider.send_email("lee@example.net", "Hello", "Hi.")
    assert not result.ok and "admin@example.org" in result.error


# ── the sender ──

@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [True, False])
async def test_a_send_that_may_have_gone_out_is_marked_so(tmp_path, uncertain):
    from sellbird.state import MAYBE_SENT_PREFIX, StateManager
    from tests.test_outbox_native import _now_iso, make_sender, seed_prospect

    class Provider:
        name = "stub"

        def is_configured(self):
            return True

        async def send_email(self, *a, **k):
            return SendResult(ok=False, error="graph 503", uncertain=uncertain)

    state = StateManager(str(tmp_path / "s.db"))
    await state.init_db()
    pid = await seed_prospect(state, email="recipient@example.invalid")
    iid = await state.add_outbox_item(
        prospect_id=pid, to_email="recipient@example.invalid", subject="Synthetic",
        body="Hello, would this help your team?", send_at=_now_iso(), status="approved")
    await make_sender(state, Provider())._drain_due()
    item = await state.get_outbox_item(iid)
    assert item["status"] == "failed"
    assert item["error"].startswith(MAYBE_SENT_PREFIX) is uncertain
