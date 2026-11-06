"""HTTP for untrusted public websites, with a boundary around the local network.

Each request (including redirects) resolves and checks every address, then
connects to a checked numeric IP. Host and TLS SNI retain the original domain.
Checking DNS and then connecting to the hostname would allow DNS rebinding.
Environment proxies are disabled so they cannot resolve or reroute the target.
Fixed mail/API integrations keep their own clients and authentication settings.
"""

import asyncio
import ipaddress
import socket
from time import monotonic

import httpx


class UnsafeURLError(httpx.RequestError):
    """The requested destination is outside the public website boundary."""


_TRANSLATION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def _public_address(value: str) -> str:
    """Reject special-use addresses, including IPv6 routes to embedded IPv4."""
    if "%" in value:
        raise ValueError("Scoped addresses are not public website targets")
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified):
        raise ValueError("Address is not public")
    if isinstance(address, ipaddress.IPv6Address):
        if (address.is_site_local or address.ipv4_mapped or address.sixtofour or address.teredo
                or any(address in network for network in _TRANSLATION_NETWORKS)):
            raise ValueError("Translated IPv6 addresses are not supported")
    return str(address)


class PublicHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        # Different domains may resolve to the same IP. Do not reuse a TLS
        # connection checked for one domain for a different domain's request.
        self._transport = httpx.AsyncHTTPTransport(
            trust_env=False, limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host = url.raw_host.decode("ascii")
        if (url.scheme not in {"http", "https"} or not host
                or url.username or url.password or "%" in host
                or host.rstrip(".").lower() == "localhost"
                or host.rstrip(".").lower().endswith(".localhost")):
            raise UnsafeURLError("Only public HTTP(S) websites are allowed", request=request)
        port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise UnsafeURLError("Invalid website port", request=request)
        timeout = request.extensions.get("timeout", {}).get("connect")
        connect_budget = timeout if timeout is not None else 10.0
        deadline = monotonic() + connect_budget

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        try:
            if literal is not None:
                addresses = [_public_address(host)]
            else:
                # Include DNS time in the client's connect budget, with a
                # finite default even if the caller disables other timeouts.
                answers = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                    timeout=connect_budget,
                )
                addresses = list(dict.fromkeys(_public_address(row[4][0]) for row in answers))
                if not addresses:
                    raise ValueError("DNS returned no addresses")
                # Many operator machines have no IPv6 route even when DNS
                # publishes AAAA first. Prefer a validated IPv4 answer.
                addresses.sort(key=lambda value: ipaddress.ip_address(value).version)
        except ValueError as exc:
            raise UnsafeURLError("Website resolves to a non-public destination", request=request) from exc
        except asyncio.TimeoutError as exc:
            raise httpx.ConnectTimeout("Website DNS resolution timed out", request=request) from exc
        except OSError as exc:
            raise httpx.ConnectError("Website DNS resolution failed", request=request) from exc

        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        for index, address in enumerate(addresses):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise httpx.ConnectTimeout("Website connection budget exhausted", request=request)
            extensions = {
                **request.extensions, "sni_hostname": host,
                "timeout": {**request.extensions.get("timeout", {}), "connect": remaining},
            }
            pinned = httpx.Request(
                method=request.method, url=url.copy_with(host=address),
                headers=headers, stream=request.stream, extensions=extensions,
            )
            try:
                return await self._transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # The request stream has not been sent. Retry only connection
                # establishment, never a read/write failure that could duplicate
                # an API operation. Every candidate was validated above.
                if index == len(addresses) - 1:
                    raise

    async def aclose(self) -> None:
        await self._transport.aclose()


def public_http_client(
    *, timeout: float | httpx.Timeout = 15.0, headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """A client whose every connection, redirect and TLS identity is checked."""
    return httpx.AsyncClient(
        transport=PublicHTTPTransport(), trust_env=False,
        timeout=timeout, headers=headers, follow_redirects=True, max_redirects=10,
    )
