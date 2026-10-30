"""A logo under every email the native providers send.

Off by default: email goes out as plain text. With
``channels.email.signature.logo`` set, Gmail, Microsoft 365 and SMTP send
each email as HTML, with the logo embedded in the message itself (an inline
``cid:`` image), so it shows without the recipient allowing remote images.
Gmail and SMTP also carry the same plain text as the HTML's alternative.

The words are the ones Harvey wrote, unchanged: the HTML is that text,
escaped, with its paragraphs and line breaks kept and its web addresses made
clickable. The pre-send gate checks that text, so the only link the HTML adds
is the logo's own ``link``, which you set.

A logo that's missing, too big or not an image never lets an email go out
without it: the sender holds the outbox (``signature_problem``) and the
providers refuse to send (``SignatureError``).
"""

import hashlib
import html
import re
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from harvey.paths import PROJECT_ROOT

# Bigger images hurt deliverability, and a logo never needs more.
MAX_LOGO_BYTES = 100_000

_URL = re.compile(r"https?://[!#-;=?-~]+")   # printable ASCII except space, " < >
_URL_TRAILING = ".,;:!?)]}'"


class SignatureError(ValueError):
    pass


@dataclass(frozen=True)
class Signature:
    data: bytes
    mime: str             # image/png, image/jpeg or image/gif
    filename: str
    content_id: str       # the src="cid:..." the HTML points at
    width: int
    height: int | None    # None when the image's size can't be read
    alt: str
    link: str
    direction: str        # rtl, ltr or auto


def _settings(config):
    email = getattr(getattr(config, "channels", None), "email", None)
    return getattr(email, "signature", None)


def load_signature(config) -> Signature | None:
    """The configured signature, or None when there isn't one. Raises
    SignatureError when there is one but its logo can't be used."""
    settings = _settings(config)
    logo = (getattr(settings, "logo", "") or "").strip()
    if not logo:
        return None
    path = Path(logo).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        size = path.stat().st_size
        if size > MAX_LOGO_BYTES:
            raise SignatureError(
                f"logo {path.name} is {size // 1000} KB; keep it under "
                f"{MAX_LOGO_BYTES // 1000} KB")
        data = path.read_bytes()
    except FileNotFoundError:
        raise SignatureError(f"logo file not found: {path}") from None
    except OSError as e:
        raise SignatureError(f"logo file can't be read: {path} ({e})") from None
    mime = _image_type(data)
    if not mime:
        raise SignatureError(f"logo {path.name} isn't a PNG, JPEG or GIF image")
    width = int(getattr(settings, "width", 120) or 120)
    size_px = _image_size(data, mime)
    height = round(width * size_px[1] / size_px[0]) if size_px and size_px[0] else None
    company = getattr(getattr(config, "persona", None), "company", "") or ""
    return Signature(
        data=data,
        mime=mime,
        filename=path.name,
        content_id=f"logo-{hashlib.sha256(data).hexdigest()[:16]}",
        width=width,
        height=height,
        alt=(getattr(settings, "alt", "") or company or "logo").strip(),
        link=(getattr(settings, "link", "") or "").strip(),
        direction=(getattr(settings, "direction", "") or "auto").strip().lower(),
    )


def signature_problem(config) -> str:
    """Why the configured signature can't be used, or "" when it can (or
    when there's none)."""
    try:
        load_signature(config)
    except SignatureError as e:
        return str(e)
    return ""


def render_html(body: str, sig: Signature) -> str:
    """The email as HTML: Harvey's text, then the logo."""
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", body.replace("\r\n", "\n").strip()) if p.strip()]
    text = "\n".join(
        f'<p style="margin:0 0 1em 0">{_paragraph(p)}</p>' for p in paragraphs
    )
    height = f' height="{sig.height}"' if sig.height else ""
    img = (
        f'<img src="cid:{sig.content_id}" width="{sig.width}"{height} '
        f'alt="{html.escape(sig.alt)}" '
        f'style="display:block;border:0;width:{sig.width}px;height:auto">'
    )
    if sig.link:
        img = f'<a href="{html.escape(sig.link)}">{img}</a>'
    if sig.direction == "auto":
        direction = 'dir="auto"'
    else:
        # Outlook's desktop renderer ignores dir="auto" and reads the style.
        align = "right" if sig.direction == "rtl" else "left"
        direction = f'dir="{sig.direction}" style="direction:{sig.direction};text-align:{align}"'
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"></head><body>\n'
        f'<div {direction}>\n'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">\n'
        f"{text}\n"
        f'<div style="margin-top:1.5em">{img}</div>\n'
        "</div>\n</div>\n</body></html>\n"
    )


def set_body(msg: EmailMessage, body: str, sig: Signature | None):
    """Plain text alone, or with a signature: plain text plus an HTML
    alternative that carries the logo as an inline image."""
    msg.set_content(body)
    if sig is None:
        return
    msg.add_alternative(render_html(body, sig), subtype="html")
    maintype, subtype = sig.mime.split("/")
    msg.get_payload()[-1].add_related(
        sig.data, maintype=maintype, subtype=subtype,
        cid=f"<{sig.content_id}>", filename=sig.filename, disposition="inline",
    )


def _paragraph(text: str) -> str:
    out, pos = [], 0
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(_URL_TRAILING)
        if len(url) <= len("https://"):
            continue
        out.append(html.escape(text[pos:m.start()], quote=False))
        out.append(f'<a href="{html.escape(url)}">{html.escape(url, quote=False)}</a>')
        pos = m.start() + len(url)
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out).replace("\n", "<br>\n")


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def _image_size(data: bytes, mime: str) -> tuple[int, int] | None:
    """(width, height) from the image's header, or None."""
    if mime == "image/png" and len(data) >= 24 and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if mime == "image/gif" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if mime == "image/jpeg":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            length = int.from_bytes(data[i + 2:i + 4], "big")
            # Start-of-frame markers carry the size; C4, C8 and CC don't.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
            i += 2 + length
    return None
