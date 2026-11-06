"""Public website requests must never connect to local/network metadata services."""

import socket

import httpx
import pytest
import pytest_asyncio

from sellbird import public_http


@pytest_asyncio.fixture
async def wire(monkeypatch):
    """Replace only the socket transport; leave the SSRF policy in place."""
    seen = []
    answers = {}
    resolutions = []

    async def resolve(host, port, **kwargs):
        resolutions.append(host)
        ips = answers.get(host, ["93.184.215.14"])
        if isinstance(ips, Exception):
            raise ips
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)) for ip in ips]

    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    def respond(request):
        seen.append(request)
        return httpx.Response(200, text="public website")

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(respond))
    return seen, answers, resolutions


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.1/", "http://2130706433/",
    "http://0x7f000001/", "http://0.0.0.0/", "http://10.0.0.1/",
    "http://172.16.0.1/", "http://192.168.1.1/", "http://169.254.169.254/",
    "http://100.100.100.200/", "http://224.0.0.1/", "http://255.255.255.255/",
    "http://[::1]/", "http://[::]/", "http://[fe80::1]/", "http://[fd00::1]/",
    "http://[fec0::1]/", "http://[feff::1]/",
    "http://[::ffff:127.0.0.1]/", "http://[ff02::1]/",
    "http://[64:ff9b::a9fe:a9fe]/", "http://[2002:7f00:1::]/",
    "http://localhost/", "http://localhost./", "http://x.localhost/",
    "https://user:password@public.test/", "ftp://public.test/file",
])
async def test_unsafe_targets_never_reach_the_network(url, wire):
    seen, answers, _ = wire
    # Non-canonical IPv4 forms are canonicalized by the system resolver.
    answers.update({"127.1": ["127.0.0.1"], "2130706433": ["127.0.0.1"],
                    "0x7f000001": ["127.0.0.1"]})
    async with public_http.public_http_client() as client:
        with pytest.raises(httpx.RequestError):
            await client.get(url)
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ips", [
    ["10.0.0.5"], ["93.184.215.14", "::1"], ["::ffff:192.168.0.1"], [],
    ["93.184.215.14", "fec0::1"], socket.gaierror("name unavailable"),
])
async def test_dns_answers_fail_closed_including_mixed_public_private(ips, wire):
    seen, answers, _ = wire
    answers["public.test"] = ips
    async with public_http.public_http_client() as client:
        with pytest.raises(httpx.RequestError):
            await client.get("https://public.test/")
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["93.184.215.14", "2606:4700:4700::1111"])
async def test_dns_is_pinned_without_losing_host_sni_or_request_url(ip, wire):
    seen, answers, resolutions = wire
    answers["public.test"] = [ip]
    async with public_http.public_http_client() as client:
        response = await client.get("https://public.test:8443/a?q=1")
    assert response.text == "public website"
    assert seen[0].url.host == ip
    assert seen[0].url.port == 8443 and seen[0].url.raw_path == b"/a?q=1"
    assert seen[0].headers["host"] == "public.test:8443"
    assert seen[0].extensions["sni_hostname"] == "public.test"
    assert str(response.request.url) == "https://public.test:8443/a?q=1"
    assert resolutions == ["public.test"]  # connection uses the IP, no second DNS lookup


@pytest.mark.asyncio
async def test_redirects_are_revalidated_before_connecting(wire, monkeypatch):
    seen, answers, resolutions = wire
    answers["internal.test"] = ["169.254.169.254"]

    def redirect(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://internal.test/latest"})

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(redirect))
    async with public_http.public_http_client() as client:
        with pytest.raises(httpx.RequestError):
            await client.get("https://public.test/")
    assert len(seen) == 1
    assert resolutions == ["public.test", "internal.test"]


@pytest.mark.asyncio
async def test_same_host_redirect_rechecks_dns_and_blocks_rebinding(wire, monkeypatch):
    seen, answers, _ = wire

    def rebind(request):
        seen.append(request)
        answers["public.test"] = ["127.0.0.1"]
        return httpx.Response(302, headers={"location": "/private"})

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(rebind))
    async with public_http.public_http_client() as client:
        with pytest.raises(httpx.RequestError):
            await client.get("https://public.test/")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_public_redirects_work_and_do_not_forward_authorization(wire, monkeypatch):
    seen, _, _ = wire

    def redirect(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"location": "https://other.test/page"})
        return httpx.Response(200, text="arrived")

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(redirect))
    async with public_http.public_http_client() as client:
        response = await client.get("https://public.test/", headers={"Authorization": "Bearer test"})
    assert response.text == "arrived"
    assert seen[1].headers["host"] == "other.test"
    assert "authorization" not in seen[1].headers
    assert str(response.url) == "https://other.test/page"


@pytest.mark.asyncio
async def test_clients_cannot_bypass_policy_using_environment_proxies(wire, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    async with public_http.public_http_client() as client:
        response = await client.get("https://public.test/")
    assert response.status_code == 200 and len(wire[0]) == 1


@pytest.mark.asyncio
async def test_production_transport_disables_cross_host_ip_pool_reuse(monkeypatch):
    options = {}

    def transport(**kwargs):
        options.update(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200))

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport", transport)
    async with public_http.public_http_client():
        pass
    assert options["limits"].max_keepalive_connections == 0
    assert options["trust_env"] is False


@pytest.mark.asyncio
async def test_ipv4_is_preferred_when_both_address_families_are_published(wire):
    seen, answers, _ = wire
    answers["public.test"] = ["2606:4700:4700::1111", "93.184.215.14"]
    async with public_http.public_http_client() as client:
        await client.get("https://public.test/")
    assert seen[0].url.host == "93.184.215.14"


@pytest.mark.asyncio
async def test_dns_timeout_is_reported_as_connect_timeout(wire):
    wire[1]["public.test"] = TimeoutError("slow DNS")
    async with public_http.public_http_client() as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get("https://public.test/")
    assert wire[0] == []


@pytest.mark.asyncio
async def test_connect_failure_tries_other_validated_addresses(wire, monkeypatch):
    seen, answers, _ = wire
    answers["public.test"] = ["93.184.215.14", "2606:4700:4700::1111"]

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(200, text="reachable")

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(respond))
    async with public_http.public_http_client() as client:
        response = await client.get("https://public.test/")
    assert response.text == "reachable"
    assert [r.url.host for r in seen] == answers["public.test"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_address_fallback_shares_one_connect_budget(wire, monkeypatch, error):
    seen, answers, _ = wire
    answers["public.test"] = ["93.184.215.14", "93.184.215.15"]
    times = iter([0.0, 0.2, 0.7])
    monkeypatch.setattr(public_http, "monotonic", lambda: next(times))

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            raise error("unreachable", request=request)
        return httpx.Response(200)

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(respond))
    async with public_http.public_http_client(timeout=1.0) as client:
        assert (await client.get("https://public.test/")).status_code == 200
    assert seen[0].extensions["timeout"]["connect"] == pytest.approx(0.8)
    assert seen[1].extensions["timeout"]["connect"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_address_fallback_stops_when_connect_budget_is_spent(wire, monkeypatch):
    seen, answers, _ = wire
    answers["public.test"] = ["93.184.215.14", "93.184.215.15"]
    times = iter([0.0, 0.2, 1.1])
    monkeypatch.setattr(public_http, "monotonic", lambda: next(times))

    def fail(request):
        seen.append(request)
        raise httpx.ConnectTimeout("slow connection", request=request)

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(fail))
    async with public_http.public_http_client(timeout=1.0) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get("https://public.test/")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_write_failures_are_never_replayed_to_another_address(wire, monkeypatch):
    seen, answers, _ = wire
    answers["public.test"] = ["93.184.215.14", "93.184.215.15"]

    def fail(request):
        seen.append(request)
        raise httpx.WriteError("connection lost after sending", request=request)

    monkeypatch.setattr(public_http.httpx, "AsyncHTTPTransport",
                        lambda **kwargs: httpx.MockTransport(fail))
    async with public_http.public_http_client() as client:
        with pytest.raises(httpx.WriteError):
            await client.post("https://public.test/", json={"query": "roofers"})
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_public_ip_literal_is_checked_without_dns(wire):
    async with public_http.public_http_client() as client:
        assert (await client.get("https://93.184.215.14/")).status_code == 200
    assert wire[2] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["scout", "profile", "trainer"])
async def test_real_website_callers_block_private_targets(caller, wire):
    if caller == "scout":
        from sellbird.agents.scout import Scout
        scout = Scout.__new__(Scout)
        assert await scout._fetch("http://127.0.0.1/", retries=1) is None
    elif caller == "profile":
        from sellbird.collectors.profile import ProfileCollector
        async with ProfileCollector(None) as collector:
            assert await collector._get("http://127.0.0.1/") == ""
    else:
        from sellbird.trainer import FallbackCrawler
        assert await FallbackCrawler().crawl("http://127.0.0.1/", max_pages=1) == {}
    assert wire[0] == []
