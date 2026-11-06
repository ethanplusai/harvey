"""Gmail MIME bodies are base64url bytes in each part's declared charset."""

import base64

import pytest

from sellbird.integrations.gmail import GmailProvider


def _part(body: bytes, content_type: str | None = None, mime_type="text/plain"):
    part = {
        "mimeType": mime_type,
        "body": {"data": base64.urlsafe_b64encode(body).decode().rstrip("=")},
    }
    if content_type is not None:
        part["headers"] = [{"name": "cOnTeNt-TyPe", "value": content_type}]
    return part


@pytest.mark.parametrize("charset, text", [
    ("UTF-8", "Je préfère mardi à 14h.\n"),
    ("ISO-8859-1", "Je préfère mardi à 14h.\n"),
    ("WINDOWS-1252", "Le prix est de 20 € pour l’été.\n"),
    ("UTF-16", "火曜日でお願いします。\n"),
])
def test_extract_text_uses_declared_charset(charset, text):
    part = _part(text.encode(charset), f'text/plain; CHARSET="{charset}"')
    assert GmailProvider._extract_text(part) == text


@pytest.mark.parametrize("content_type", [
    None,
    "text/plain",
    'text/plain; charset=""',
    "text/plain; charset",
    "text/plain; charset=unknown-charset",
    'text/plain; charset="utf-8\0"',
    "text/plain; charset=base64_codec",
    'text/plain; charset="☃"',
])
def test_extract_text_falls_back_to_utf8(content_type):
    part = _part("café".encode() + b"\xff", content_type)
    assert GmailProvider._extract_text(part) == "café�"


def test_extract_text_replaces_invalid_bytes_in_declared_charset():
    part = _part(b"caf\xe9", "text/plain; charset=us-ascii")
    assert GmailProvider._extract_text(part) == "caf�"


def test_nested_parts_use_their_own_charset():
    text = "Je préfère mardi à 14h."
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "Content-Type", "value": "multipart/mixed; charset=utf-8"}],
        "parts": [
            _part(b"not the body", mime_type="application/octet-stream"),
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    _part(text.encode("iso-8859-1"), "text/plain; charset=iso-8859-1"),
                    _part(b"<p>HTML alternative</p>", mime_type="text/html"),
                ],
            },
        ],
    }
    assert GmailProvider._extract_text(payload) == text


def test_missing_child_charset_does_not_inherit_parent_charset():
    payload = {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "Content-Type", "value": "multipart/alternative; charset=iso-8859-1"}],
        "parts": [_part("café".encode())],
    }
    assert GmailProvider._extract_text(payload) == "café"


def test_html_fallback_decodes_before_stripping_tags():
    part = _part(
        "<p>Ça coûte 20 €.</p>".encode("windows-1252"),
        "text/html; charset=windows-1252", mime_type="text/html",
    )
    assert GmailProvider._extract_text({"mimeType": "multipart/alternative", "parts": [part]}) == (
        " Ça coûte 20 €. "
    )


@pytest.mark.parametrize("body", [{}, {"attachmentId": "attachment"}, {"data": "a"}])
def test_missing_or_invalid_body_data_remains_empty(body):
    part = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
        "body": body,
    }
    assert GmailProvider._extract_text(part) == ""


def test_parse_message_preserves_decoded_reply():
    text = "Je préfère mardi à 14h."
    payload = _part(text.encode("iso-8859-1"), "text/plain; charset=iso-8859-1")
    payload["headers"].extend([
        {"name": "From", "value": "Customer <reader@example.invalid>"},
        {"name": "Subject", "value": "Re: Meeting"},
    ])
    message = GmailProvider._parse_message({
        "id": "reply-1", "threadId": "thread-1", "payload": payload,
        "snippet": "Snippet should not replace the body",
    })
    assert message is not None
    assert message.body == text
    assert message.from_email == "reader@example.invalid"
    assert message.thread_ref == "thread-1"


@pytest.mark.parametrize("headers", [
    ["not a header", {"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
    [{"name": None, "value": 3}, {"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
    "Content-Type: text/plain",
    None,
])
def test_malformed_part_headers_never_lose_the_reply(headers):
    """An unsubscribe must survive odd headers: before #58 nothing here could
    fail, so a failure must not turn into an empty body."""
    import base64
    from sellbird.integrations.gmail import GmailProvider
    raw = "Please unsubscribe me. Merci, très bien.".encode("iso-8859-1")
    part = {"mimeType": "text/plain", "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(raw).decode()}}
    text = GmailProvider._extract_text(part)
    assert "unsubscribe" in text


@pytest.mark.parametrize("charset", ["punycode", "idna", "utf-7", "unicode_escape", "raw-unicode-escape"])
def test_codecs_that_arent_mail_charsets_fall_back_to_utf8(charset):
    """punycode is quadratic (a few MB stalled the heartbeat for minutes) and
    turned "unsubscribe" into garbage; the escape codecs let a sender shape
    the text. None is a real mail charset."""
    import time
    body = b"Please unsubscribe me. " + b"9" * 300_000
    started = time.perf_counter()
    text = GmailProvider._extract_text(_part(body, f"text/plain; charset={charset}"))
    assert time.perf_counter() - started < 1
    assert text.startswith("Please unsubscribe me.")


def test_a_huge_part_is_only_decoded_as_far_as_sellbird_reads():
    body = "Please unsubscribe me. ".encode("iso-8859-1") + b"x" * 5_000_000
    text = GmailProvider._extract_text(_part(body, "text/plain; charset=iso-8859-1"))
    assert text.startswith("Please unsubscribe me.") and len(text) <= 200_000
