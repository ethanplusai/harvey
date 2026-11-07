"""Send hooks: what an extension may add to the decision to send, and to the email."""

from datetime import datetime, timezone

import pytest

from sellbird import extensions
from sellbird.integrations.mail_provider import SendResult
from sellbird.send_hooks import (
    NO_HEADERS_HOLD, Cancel, Hold, SendCheck, SendHookError, extra_headers, first_verdict,
)
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_prospect, state,
)


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(extensions, "_REGISTRY", extensions.Registry())
    monkeypatch.setattr(extensions, "_LOADED", True)
    return extensions._REGISTRY


class HeaderProvider(FakeProvider):
    carries_headers = True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", headers=()):
        result = await super().send_email(to_email, subject, body, thread_ref, in_reply_to)
        self.sent[-1]["headers"] = list(headers)
        return result


async def _queued(state, email="jane@acme.com", kind="sequence"):
    pid = await seed_prospect(state, email=email)
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="hello there",
        body="A short, plain note. Question?", send_at=_now_iso(), status="approved",
        kind=kind, step=1)


# ── rules ──

@pytest.mark.asyncio
async def test_a_rule_can_cancel_or_hold(state, registry):
    seen = []

    async def warm(check: SendCheck):
        seen.append((check.item["to_email"], check.cold, check.prospect.email))
        if check.item["to_email"].startswith("never"):
            return Cancel("on the partner's list")
        if check.item["to_email"].startswith("later"):
            return Hold("this inbox is still warming")
        return None

    registry.add_send_rule("ext.warm", warm)
    never, later, fine = (await _queued(state, "never@acme.com"),
                          await _queued(state, "later@acme.com"),
                          await _queued(state, "fine@acme.com"))
    sender = make_sender(state, FakeProvider())
    await sender._drain_due()
    assert [m["to"] for m in sender.provider.sent] == ["fine@acme.com"]
    row = await state.get_outbox_item(never)
    assert row["status"] == "cancelled" and row["error"] == "ext.warm: on the partner's list"
    row = await state.get_outbox_item(later)
    assert row["status"] == "approved" and "held: ext.warm: this inbox is still warming" in row["error"]
    assert ("fine@acme.com", True, "fine@acme.com") in seen


@pytest.mark.asyncio
async def test_rules_never_run_on_an_email_the_built_in_checks_stopped(state, registry):
    asked = []

    async def anything(check):
        asked.append(check.item["to_email"])
        return None

    registry.add_send_rule("ext.any", anything)
    item = await _queued(state)
    await state.add_suppression("address", "jane@acme.com", "manual")
    await make_sender(state, FakeProvider())._drain_due()
    assert asked == [] and (await state.get_outbox_item(item))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_rule_that_raises_holds_the_email():
    async def broken(check):
        raise RuntimeError("vendor down")

    async def odd(check):
        return "yes"

    assert await first_verdict({"ext.broken": broken}, None) == (
        "approved", "held: ext.broken couldn't check it")
    assert await first_verdict({"ext.odd": odd}, None) == (
        "approved", "held: ext.odd gave no clear answer")


def test_rules_must_be_async_and_named_once(registry):
    with pytest.raises(extensions.ExtensionError, match="async"):
        registry.add_send_rule("ext.sync", lambda check: None)

    async def ok(check):
        return None
    registry.add_send_rule("ext.ok", ok)
    with pytest.raises(extensions.ExtensionError, match="already"):
        registry.add_send_rule("ext.ok", ok)


# ── headers ──

def _unsubscribe(item):
    return [("List-Unsubscribe", f"<https://example.invalid/u/{item['id']}>"),
            ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")]


@pytest.mark.asyncio
async def test_headers_reach_a_provider_that_carries_them(state, registry):
    registry.add_send_headers("ext.unsubscribe", _unsubscribe)
    item = await _queued(state)
    sender = make_sender(state, HeaderProvider())
    await sender._drain_due()
    assert sender.provider.sent[0]["headers"] == [
        ("List-Unsubscribe", f"<https://example.invalid/u/{item}>"),
        ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")]


@pytest.mark.asyncio
async def test_an_email_that_needs_headers_waits_for_an_inbox_that_can_carry_them(state, registry):
    registry.add_send_headers("ext.unsubscribe", _unsubscribe)
    item = await _queued(state)
    sender = make_sender(state, FakeProvider())   # carries_headers is False
    await sender._drain_due()
    assert sender.provider.sent == []
    row = await state.get_outbox_item(item)
    assert row["status"] == "approved" and NO_HEADERS_HOLD in row["error"]


@pytest.mark.asyncio
async def test_no_header_hooks_means_no_headers_argument(state, registry):
    await _queued(state)
    sender = make_sender(state, FakeProvider())   # its send_email takes no headers=
    await sender._drain_due()
    assert len(sender.provider.sent) == 1


@pytest.mark.parametrize("header,value", [
    ("From", "someone@example.invalid"), ("subject", "x"), ("Bad Name", "x"),
    ("X-Ok", "line\r\nBcc: spy@example.invalid"),
])
def test_reserved_or_malformed_headers_are_refused(header, value):
    with pytest.raises(SendHookError):
        extra_headers({"ext.bad": lambda item: [(header, value)]}, {"id": "o1"})


@pytest.mark.asyncio
async def test_a_bad_header_holds_the_email(state, registry):
    registry.add_send_headers("ext.bad", lambda item: [("From", "spoof@example.invalid")])
    item = await _queued(state)
    sender = make_sender(state, HeaderProvider())
    await sender._drain_due()
    assert sender.provider.sent == []
    assert (await state.get_outbox_item(item))["status"] == "approved"


@pytest.mark.asyncio
async def test_microsoft_refuses_custom_headers_rather_than_dropping_them():
    from types import SimpleNamespace

    from sellbird.integrations.microsoft import MicrosoftProvider
    provider = MicrosoftProvider(SimpleNamespace(persona=SimpleNamespace(name="", email="")),
                                 SimpleNamespace())
    assert provider.carries_headers is False
    result = await provider.send_email("a@example.invalid", "s", "b", headers=[("X-A", "1")])
    assert isinstance(result, SendResult) and not result.ok


def test_gmail_and_smtp_carry_headers():
    from sellbird.integrations.gmail import GmailProvider
    from sellbird.integrations.smtp_mail import SmtpImapProvider
    assert GmailProvider.carries_headers and SmtpImapProvider.carries_headers


@pytest.mark.asyncio
async def test_gmail_writes_the_headers_into_the_message():
    import base64
    import email

    from sellbird.integrations import gmail
    from tests.test_mail_identity import _config, _env, _gmail_transport, _Tokens
    sent = []
    provider = gmail.GmailProvider(_config(), _env(), _gmail_transport(sent),
                                   tokens=_Tokens({"refresh_token": "r"}))
    assert (await provider.send_email("lee@example.net", "Hello", "Hi.",
                                      headers=[("List-Unsubscribe", "<mailto:u@example.invalid>")])).ok
    msg = email.message_from_bytes(base64.urlsafe_b64decode(sent[0]["raw"]))
    assert msg["List-Unsubscribe"] == "<mailto:u@example.invalid>"
