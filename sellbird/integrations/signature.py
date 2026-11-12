"""A logo under every email the native providers send.

Off by default: email goes out as plain text. With
``channels.email.signature.logo`` set, Gmail, Microsoft 365 and SMTP send
each email as HTML, with the logo embedded in the message itself (an inline
``cid:`` image), so it shows without the recipient allowing remote images.
Gmail and SMTP also carry the same plain text as the HTML's alternative.

When the email ends with the sender's name on a line of its own (the
sign-off: the first name the writer adds under every cold email, or a closing
paragraph that opens with ``persona.name`` when operator email rules write
their own), the logo sits beside those lines, and the company's name in them
opens ``link``. Otherwise the logo sits under the words. The compliance
footer the sender adds to cold email (postal address and opt-out line) comes
last, in smaller grey type, the way a footer reads in any email client.

The words are the ones Sellbird wrote, unchanged: the HTML is that text,
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

from sellbird.config import compliance_footer_lines
from sellbird.paths import PROJECT_ROOT

# Bigger images hurt deliverability, and a logo never needs more.
MAX_LOGO_BYTES = 100_000
MAX_LOGO_PIXELS = 20_000

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
    height: int
    alt: str
    link: str
    direction: str        # rtl, ltr or auto
    footer: str = ""      # the compliance paragraph the sender appends, as written
    name: str = ""        # persona.name: a closing paragraph starting with it, or its first word, is the sign-off
    company: str = ""     # persona.company: linked inside the sign-off when link is set


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
    try:
        path = Path(logo).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        size = path.stat().st_size
        if size > MAX_LOGO_BYTES:
            raise SignatureError(
                f"logo {path.name} is {size // 1000} KB; keep it under "
                f"{MAX_LOGO_BYTES // 1000} KB")
        data = path.read_bytes()
    except FileNotFoundError:
        raise SignatureError(f"logo file not found: {path}") from None
    except (OSError, RuntimeError, ValueError) as e:   # ~nouser, a NUL in the path
        raise SignatureError(f"logo file can't be read: {logo} ({e})") from None
    mime = _image_type(data)
    if not mime:
        raise SignatureError(f"logo {path.name} isn't a PNG, JPEG or GIF image")
    width = int(getattr(settings, "width", 120) or 120)
    size_px = _image_size(data, mime)
    # A header with no usable size is a corrupt file, which would show as a
    # broken image in every email.
    if not size_px or not all(0 < n <= MAX_LOGO_PIXELS for n in size_px):
        raise SignatureError(f"logo {path.name} is a damaged image")
    height = round(width * size_px[1] / size_px[0])
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
        footer="\n".join(compliance_footer_lines(config)),
        name=(getattr(getattr(config, "persona", None), "name", "") or "").strip(),
        company=company.strip(),
    )


def signature_problem(config) -> str:
    """Why the configured signature can't be used, or "" when it can (or
    when there's none)."""
    try:
        load_signature(config)
    except SignatureError as e:
        return str(e)
    return ""


MAX_SIGN_OFF_LINES = 4


def render_html(body: str, sig: Signature) -> str:
    """The email as HTML: Sellbird's text, the logo beside the sign-off (or
    under the text when there's no sign-off), then on cold email the
    compliance footer in smaller type."""
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", body.replace("\r\n", "\n").strip()) if p.strip()]
    footer = ""
    if sig.footer and paragraphs and paragraphs[-1].strip() == sig.footer:
        footer = (
            '<div style="margin-top:1.5em;font-size:12px;line-height:1.4;color:#6b6b6b">'
            f"{_paragraph(paragraphs.pop().strip())}</div>\n"
        )
    sign_off = []
    if sig.name and paragraphs:
        lines = [ln.strip() for ln in paragraphs[-1].strip().split("\n")]
        first = sig.name.split(" ")[0]
        if lines[0] in (sig.name, first) and len(lines) <= MAX_SIGN_OFF_LINES:
            sign_off = lines
            paragraphs.pop()
    text = "\n".join(
        f'<p style="margin:0 0 1em 0">{_paragraph(p)}</p>' for p in paragraphs
    )
    height = f' height="{sig.height}"'
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
    if sign_off:
        closing = (
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'style="border-collapse:collapse;margin-top:1em"><tr>\n'
            f'<td style="vertical-align:middle;padding:0 12px 0 0">{img}</td>\n'
            '<td style="vertical-align:middle;padding:0;font-family:Arial,Helvetica,sans-serif;'
            'font-size:14px;line-height:1.5">'
            + "<br>\n".join(_sign_off_line(ln, sig) for ln in sign_off)
            + "</td>\n</tr></table>\n"
        )
    else:
        closing = f'<div style="margin-top:1.5em">{img}</div>\n'
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"></head><body>\n'
        f'<div {direction}>\n'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">\n'
        f"{text}\n"
        f"{closing}"
        f"{footer}"
        "</div>\n</div>\n</body></html>\n"
    )


def _sign_off_line(line: str, sig: Signature) -> str:
    """One sign-off line, escaped, with the company name opening the link."""
    out = _paragraph(line)
    company = html.escape(sig.company, quote=False)
    if sig.link and company and company in out:
        out = out.replace(company, f'<a href="{html.escape(sig.link)}">{company}</a>', 1)
    return out


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
