"""A logo under every email: loading it safely, the HTML around Harvey's
text, the MIME layout for Gmail/SMTP, and the outbox holding when the logo
can't be used. Microsoft's Graph calls are tested in test_microsoft.py."""

import email
import email.policy
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from harvey.config import SignatureConfig
from harvey.integrations import signature
from harvey.integrations.signature import (
    MAX_LOGO_BYTES,
    SignatureError,
    load_signature,
    render_html,
    set_body,
    signature_problem,
)
from email.message import EmailMessage


def png(width=2, height=1) -> bytes:
    """A real (tiny, blank) PNG of the given size."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def config(company="Acme", **settings):
    return SimpleNamespace(
        persona=SimpleNamespace(company=company),
        channels=SimpleNamespace(email=SimpleNamespace(signature=SignatureConfig(**settings))),
    )


@pytest.fixture
def logo(tmp_path):
    path = tmp_path / "logo.png"
    path.write_bytes(png(200, 100))
    return path


# ── loading ──


def test_no_logo_means_no_signature():
    assert load_signature(config()) is None
    assert load_signature(None) is None
    # Configs from before the setting existed have no signature attribute.
    assert load_signature(SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace()))) is None
    assert signature_problem(config()) == ""


def test_logo_is_read_with_its_size(logo):
    sig = load_signature(config(logo=str(logo), width=120))

    assert sig.mime == "image/png" and sig.data == logo.read_bytes()
    assert (sig.width, sig.height) == (120, 60)  # keeps the 2:1 shape
    assert sig.alt == "Acme"                     # persona.company by default
    assert sig.content_id.startswith("logo-")
    assert sig.content_id == load_signature(config(logo=str(logo))).content_id


def test_relative_logo_is_found_in_the_project_folder(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "brand.png").write_bytes(png())
    monkeypatch.setattr(signature, "PROJECT_ROOT", tmp_path)

    assert load_signature(config(logo="assets/brand.png")).filename == "brand.png"


def test_jpeg_and_gif_sizes_are_read(tmp_path):
    gif = tmp_path / "logo.gif"
    gif.write_bytes(b"GIF89a" + struct.pack("<HH", 300, 100) + b"\x00" * 20)
    # SOI, an APP0 segment to skip, then a baseline frame header (SOF0).
    jpeg = tmp_path / "logo.jpg"
    jpeg.write_bytes(b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00"
                     + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 50, 200) + b"\x00" * 12)

    assert load_signature(config(logo=str(gif), width=150)).height == 50
    sig = load_signature(config(logo=str(jpeg), width=100))
    assert sig.mime == "image/jpeg" and sig.height == 25


@pytest.mark.parametrize("make,why", [
    (lambda p: None, "not found"),
    (lambda p: p.write_text("not an image"), "isn't a PNG"),
    (lambda p: p.write_bytes(png() + b"\x00" * MAX_LOGO_BYTES), "keep it under"),
])
def test_unusable_logo_is_refused(tmp_path, make, why):
    path = tmp_path / "logo.png"
    make(path)

    with pytest.raises(SignatureError, match=why):
        load_signature(config(logo=str(path)))
    assert why in signature_problem(config(logo=str(path)))


@pytest.mark.parametrize("settings", [
    {"direction": "sideways"},
    {"width": 0},
    {"width": 5000},
    {"link": "javascript:alert(1)"},
    {"link": "https://acme.example/a b"},
])
def test_bad_settings_are_rejected(settings):
    with pytest.raises(ValidationError):
        SignatureConfig(**settings)


def test_settings_are_tidied():
    s = SignatureConfig(logo=None, direction=" RTL ", link=" https://acme.example ")
    assert (s.logo, s.direction, s.link) == ("", "rtl", "https://acme.example")


# ── HTML ──


def test_html_keeps_the_text_and_escapes_it(logo):
    body = "Hi Jane,\n\nWe noticed <b>your</b> studio & café.\nSecond line.\n\n\nThanks"
    out = render_html(body, load_signature(config(logo=str(logo))))

    assert "<b>your</b>" not in out
    assert "&lt;b&gt;your&lt;/b&gt; studio &amp; café.<br>\nSecond line." in out
    assert out.count('<p style="margin:0 0 1em 0">') == 3  # blank lines split paragraphs


def test_html_makes_web_addresses_clickable(logo):
    body = "Book here: https://acme.example/book?a=1&b=2. (or https://acme.example/faq)"
    out = render_html(body, load_signature(config(logo=str(logo))))

    assert '<a href="https://acme.example/book?a=1&amp;b=2">https://acme.example/book?a=1&amp;b=2</a>.' in out
    assert '(or <a href="https://acme.example/faq">https://acme.example/faq</a>)' in out


def test_html_logo_direction_and_link(logo):
    sig = load_signature(config(logo=str(logo), direction="rtl", alt="Acme logo",
                                link="https://acme.example"))
    out = render_html("שלום", sig)

    assert 'dir="rtl" style="direction:rtl;text-align:right"' in out
    assert (f'<a href="https://acme.example"><img src="cid:{sig.content_id}" width="120" '
            f'height="60" alt="Acme logo"') in out
    assert 'dir="auto"' in render_html("hi", load_signature(config(logo=str(logo))))


# ── MIME (Gmail, SMTP) ──


def test_mime_carries_plain_text_html_and_the_inline_logo(logo):
    sig = load_signature(config(logo=str(logo)))
    msg = EmailMessage()
    set_body(msg, "Hi Jane.\nThanks", sig)
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)

    assert parsed.get_content_type() == "multipart/alternative"
    plain, related = parsed.get_payload()
    assert plain.get_content().strip() == "Hi Jane.\nThanks"
    assert related.get_content_type() == "multipart/related"
    html_part, image = related.get_payload()
    assert f'src="cid:{sig.content_id}"' in html_part.get_content()
    assert image.get_content_type() == "image/png"
    assert image["Content-ID"] == f"<{sig.content_id}>"
    assert image.get_content_disposition() == "inline"
    assert image.get_content() == logo.read_bytes()


def test_mime_without_signature_is_plain_text():
    msg = EmailMessage()
    set_body(msg, "Hi Jane.", None)
    assert msg.get_content_type() == "text/plain"


@pytest.mark.asyncio
async def test_smtp_sends_the_signed_message(logo, monkeypatch):
    from harvey.integrations import smtp_mail
    sent = []
    async def fake_send(msg, **kwargs):
        sent.append(msg)
    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", fake_send)
    cfg = config(logo=str(logo))
    cfg.persona = SimpleNamespace(name="Sam", email="sam@sender.example", company="Acme")
    env = SimpleNamespace(smtp_host="smtp.sender.example", smtp_port=587,
                          smtp_username="sam@sender.example", smtp_password="x")

    result = await smtp_mail.SmtpImapProvider(cfg, env).send_email("jane@acme.example", "hi", "Hi Jane.")

    assert result.ok and sent[0].get_content_type() == "multipart/alternative"


@pytest.mark.asyncio
async def test_gmail_sends_the_signed_message(logo):
    import base64
    from harvey.integrations import gmail
    cfg = config(logo=str(logo))
    cfg.persona = SimpleNamespace(name="Sam", email="sam@sender.example", company="Acme")
    p = gmail.GmailProvider(cfg, SimpleNamespace())
    p._request = AsyncMock(side_effect=[{"id": "g1", "threadId": "t1"}, None])

    result = await p.send_email("jane@acme.example", "hi", "Hi Jane.")

    assert result.ok
    raw = p._request.call_args_list[0].kwargs["json"]["raw"]
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
    assert parsed.get_content_type() == "multipart/alternative"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["smtp", "gmail"])
async def test_unusable_logo_sends_nothing(tmp_path, provider, monkeypatch):
    from harvey.integrations import gmail, smtp_mail
    cfg = config(logo=str(tmp_path / "missing.png"))
    cfg.persona = SimpleNamespace(name="Sam", email="sam@sender.example", company="Acme")
    monkeypatch.setattr(smtp_mail.aiosmtplib, "send", AsyncMock())
    if provider == "smtp":
        p = smtp_mail.SmtpImapProvider(cfg, SimpleNamespace(smtp_host="h", smtp_username="u", smtp_password="x"))
    else:
        p = gmail.GmailProvider(cfg, SimpleNamespace())
        p._request = AsyncMock()

    result = await p.send_email("jane@acme.example", "hi", "Hi Jane.")

    assert not result.ok and result.error.startswith("signature:")
    if provider == "smtp":
        smtp_mail.aiosmtplib.send.assert_not_called()
    else:
        p._request.assert_not_called()


# ── the outbox ──


@pytest.mark.asyncio
async def test_outbox_holds_while_the_logo_is_unusable(tmp_path, logo, monkeypatch):
    from harvey.state import StateManager
    from tests.test_outbox_native import Cfg, FakeProvider, _now_iso, make_sender, seed_prospect
    state = StateManager(str(tmp_path / "state.db"))
    await state.init_db()
    pid = await seed_prospect(state)
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello", body="Hi Jane.",
        send_at=_now_iso(), status="approved", campaign_id="c1", step=1,
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    monkeypatch.setattr(Cfg.channels.email, "signature",
                        SignatureConfig(logo=str(tmp_path / "missing.png")), raising=False)

    await sender._drain_due()

    assert provider.sent == []
    [held] = await state.get_outbox(status="approved")  # still approved, not failed
    assert held["id"] == item

    monkeypatch.setattr(Cfg.channels.email, "signature", SignatureConfig(logo=str(logo)))
    await sender._drain_due()
    assert [s["to"] for s in provider.sent] == ["jane@acme.com"]
