"""aiosmtplib must refuse addresses that smuggle extra SMTP commands.

Sellbird hands aiosmtplib addresses it scraped from third-party websites, both
when sending (smtp_mail) and when probing a mailbox (email_finder). Versions
before 5.1.1 wrote a CR/LF inside an address straight onto the wire, so
"a@b.com\\r\\nRCPT TO:<x@y.com>" became two commands (GHSA-v3q9-hj7j-63hq).
This pins the behaviour the dependency floor in pyproject.toml exists for.
"""

import asyncio

import aiosmtplib
import pytest

INJECTED = "a@example.com\r\nRCPT TO:<victim@example.org>"


async def _fake_smtp_server():
    """A minimal SMTP server on loopback that records every line it gets."""
    received: list[str] = []

    async def handle(reader, writer):
        writer.write(b"220 fake ESMTP\r\n")
        await writer.drain()
        while line := await reader.readline():
            text = line.decode(errors="replace").strip()
            received.append(text)
            if text.upper().startswith("EHLO"):
                writer.write(b"250 fake\r\n")
            elif text.upper() == "QUIT":
                writer.write(b"221 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"250 ok\r\n")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], received


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["mail", "rcpt"])
async def test_crlf_in_address_never_reaches_the_server(command):
    server, port, received = await _fake_smtp_server()
    smtp = aiosmtplib.SMTP(hostname="127.0.0.1", port=port, timeout=5,
                          local_hostname="localhost")
    outcome = None
    try:
        await smtp.connect()
        await smtp.ehlo()
        if command == "rcpt":
            await smtp.mail("sender@example.com")
        try:
            # Bounded: a vulnerable version sends two commands, the replies
            # fall out of step, and the call would otherwise just wait.
            await asyncio.wait_for(getattr(smtp, command)(INJECTED), 5)
        except Exception as e:
            outcome = e
    finally:
        smtp.close()
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), 2)
        except asyncio.TimeoutError:
            pass

    assert not any("victim@example.org" in line for line in received), received
    # Either is safe: aiosmtplib refuses the address, or (on Pythons whose
    # email.utils.parseaddr cuts it at the line break, like 3.11.9) only the
    # clean address goes out. Anything else, or anything smuggled, is not.
    verb = "MAIL FROM:" if command == "mail" else "RCPT TO:"
    sent = [line for line in received if line.startswith(verb)][-1:]
    refused = isinstance(outcome, ValueError) and sent == []
    trimmed = outcome is None and sent == [f"{verb}<a@example.com>"]
    assert refused or trimmed, (outcome, received)
