#!/usr/bin/env python
"""Why is Overpass failing right now? — a read-only probe of the mirrors.

Harvey's OpenStreetMap provider walks ``OpenStreetMap.ENDPOINTS`` and gives up
with one message for every kind of failure, which is exactly the information
you do not have when a run comes back empty. This script separates the three
possible culprits:

* **the public servers** — a 429/5xx answer, or no answer at all;
* **this machine's network** — DNS that does not resolve, a connection that
  never opens, a connection that opens and then hangs;
* **Harvey's own code** — ruled in or out by whether a minimal query, sent the
  same way Harvey sends its queries, succeeds here.

It is deliberately inert: it never opens the Harvey database, never writes a
company or an observation, never runs a pipeline stage, and never sends mail.
It imports ``harvey.collectors.discover`` only to read the endpoint list and
the retry statuses, so what is probed is what Harvey is actually configured
with. That import pulls in ``harvey.models`` and nothing that connects.

Network discipline, because these are volunteer servers:

* hosts that resolve to the same addresses are one instance, and are probed
  once — an alias is not a second chance;
* one DNS lookup, one TCP connect and at most one Overpass query per instance;
* that query is ``node(1);out;`` — a single node by id, no geographic area —
  or, with ``--coverage``, a tally of the cafes in one square kilometre of
  Denver, which separates a full-planet instance from a regional extract;
* ``--only`` narrows a run to one mirror, so vetting a candidate costs the
  others nothing;
* no retries, no loops, and a short read timeout.

Read the output as a snapshot of this minute from this machine. A mirror that
fails one probe is a mirror that failed once; it is not proof the service is
down, and a successful TCP connect is not proof the API is answering.

Usage (nothing happens until one of these is run):

    .venv/Scripts/python.exe scripts/diagnose_overpass.py
    .venv/Scripts/python.exe scripts/diagnose_overpass.py --dns-only
    .venv/Scripts/python.exe scripts/diagnose_overpass.py --read-timeout 120

Vetting a candidate mirror before it goes into ENDPOINTS — one query, and the
configured mirrors are left alone:

    .venv/Scripts/python.exe scripts/diagnose_overpass.py --coverage
        --only candidate.example https://candidate.example/api/interpreter
"""

import argparse
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harvey.collectors.discover import OpenStreetMap  # noqa: E402

# The smallest valid Overpass query there is: one node, by id, no area filter.
QUERY = "[out:json][timeout:10];node(1);out;"
# Is this instance the whole planet, or a regional extract? A community
# mirror can be independent, fast and still hold one country, which would
# answer "roofers in Denver" with an empty list — the one failure this
# provider must never produce. ``out count`` returns a tally, not the data,
# so asking is cheap: a few cafes in downtown Denver, 1km radius.
COVERAGE = ("[out:json][timeout:25];"
            "node[amenity=cafe](around:1000,39.7392,-104.9903);out count;")
HEADERS = {"User-Agent": "Harvey/0.1 diagnostics (one-off mirror probe; "
                         "github.com/ethanplusai/harvey)"}


def resolve(host):
    """Addresses for ``host``, or the DNS error that stopped us."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return None, f"DNS failure: {e.strerror or e}"
    return sorted({i[4][0] for i in infos}), ""


def tcp_probe(ip, timeout=5.0):
    """Does a socket to :443 open at all? Says nothing about the API."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((ip, 443), timeout=timeout):
            pass
    except OSError as e:
        return f"no TCP: {e.strerror or e}", time.perf_counter() - t0
    return "TCP open", time.perf_counter() - t0


def classify(e):
    """The useful name for a transport failure, which str(e) often is not."""
    if isinstance(e, httpx.ConnectTimeout):
        return "connect timeout — nothing answered the handshake"
    if isinstance(e, httpx.ReadTimeout):
        return "read timeout — connected, then no response in time"
    if isinstance(e, httpx.WriteTimeout):
        return "write timeout — connected, could not send the query"
    if isinstance(e, httpx.PoolTimeout):
        return "pool timeout — client-side, not the server"
    if isinstance(e, httpx.ConnectError):
        detail = str(e) or e.__class__.__name__
        if "getaddrinfo" in detail or "Name or service" in detail:
            return f"DNS failure inside httpx: {detail}"
        return f"connection refused or reset: {detail}"
    if isinstance(e, httpx.RemoteProtocolError):
        return f"protocol error — the server hung up mid-response: {e}"
    return f"{e.__class__.__name__}: {e or '(no detail)'}"


def query_once(client, url, query=QUERY):
    """One minimal Overpass query. No retry — that is Harvey's job, not ours."""
    t0 = time.perf_counter()
    try:
        r = client.post(url, content=query.encode(), headers=HEADERS)
    except httpx.HTTPError as e:
        return None, classify(e), time.perf_counter() - t0
    return r, "", time.perf_counter() - t0


def describe(r):
    """What the answer tells us, beyond the number."""
    retryable = r.status_code in OpenStreetMap.RETRY_STATUSES
    verdict = ("Harvey treats this as transient and walks to the next mirror"
               if retryable else "Harvey fails the query here, by design")
    if r.status_code == 200:
        try:
            body = r.json()
        except ValueError:
            return "HTTP 200 but the body is not JSON — a portal or a proxy?"
        remark = (body.get("remark") or "").strip()
        els = body.get("elements") or []
        if len(els) == 1 and els[0].get("type") == "count":
            total = (els[0].get("tags") or {}).get("total", "?")
            verdict = ("holds data outside Europe — full planet"
                       if total not in ("0", "?") else
                       "NO data for that area — a regional extract, unusable "
                       "as a mirror")
            return f"HTTP 200, count={total} — {verdict}"
        out = (f"HTTP 200, valid JSON, {len(els)} element(s) — this instance "
               f"is answering")
        return f"{out}; server remark: {remark}" if remark else out
    first = " ".join((r.text or "").split())[:140]
    return f"HTTP {r.status_code} — {verdict}" + (f"; body: {first}" if first else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dns-only", action="store_true",
                    help="resolve and group the mirrors, send nothing")
    ap.add_argument("--read-timeout", type=float, default=20.0,
                    help="seconds to wait for a response body (default 20)")
    ap.add_argument("--coverage", action="store_true",
                    help="ask for a tally of cafes in Denver instead of one "
                         "node, to tell a full-planet instance from a "
                         "regional extract")
    ap.add_argument("--only", default="",
                    help="probe only the endpoints whose URL contains this "
                         "text, so checking one mirror costs the others "
                         "nothing")
    ap.add_argument("extra", nargs="*",
                    help="candidate endpoint URLs to probe as well as the "
                         "configured ones (each costs one more query)")
    args = ap.parse_args()

    endpoints = list(OpenStreetMap.ENDPOINTS) + list(args.extra)
    if args.only:
        endpoints = [u for u in endpoints if args.only in u]
        if not endpoints:
            print(f"No configured or given endpoint contains {args.only!r}.")
            return 2
        print(f"--only {args.only!r}: probing {len(endpoints)} endpoint(s); "
              f"the others are left alone.")
    print(f"Configured endpoints ({len(OpenStreetMap.ENDPOINTS)}) "
          f"+ extra ({len(args.extra)}); retryable statuses: "
          f"{sorted(OpenStreetMap.RETRY_STATUSES)}\n")

    # ── resolve first, so that aliases are never probed twice ──
    rows = []
    for url in endpoints:
        host = urlparse(url).hostname or url
        ips, err = resolve(host)
        rows.append({"url": url, "host": host, "ips": ips, "dns_error": err})
        print(f"  {host:<34} {err or ', '.join(ips)}")

    groups, order = {}, []
    for row in rows:
        key = frozenset(row["ips"]) if row["ips"] else f"unresolved:{row['host']}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    print(f"\n{len(order)} apparently independent instance(s) behind "
          f"{len(rows)} URL(s):")
    for key in order:
        members = groups[key]
        names = ", ".join(m["host"] for m in members)
        print(f"  - {names}" + ("  (same addresses -> one instance, probed once)"
                                if len(members) > 1 else ""))
    if args.dns_only:
        print("\n--dns-only: no TCP connection and no query was sent.")
        return 0

    # ── one TCP connect and one query per instance ──
    print()
    timeout = httpx.Timeout(connect=5.0, read=args.read_timeout,
                            write=5.0, pool=5.0)
    results = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for key in order:
            row = groups[key][0]
            host, url = row["host"], row["url"]
            print(f"-- {host}")
            print(f"   url   {url}")
            if not row["ips"]:
                print(f"   dns   {row['dns_error']}")
                print("   skip  cannot probe a name that does not resolve\n")
                results.append((host, row["dns_error"]))
                continue
            print(f"   dns   {', '.join(row['ips'])}")
            tcp, tcp_s = tcp_probe(row["ips"][0])
            print(f"   tcp   {tcp}  ({tcp_s:.2f}s, port 443 — not an API check)")
            r, err, secs = query_once(client, url,
                                      COVERAGE if args.coverage else QUERY)
            outcome = err or describe(r)
            print(f"   api   {outcome}  ({secs:.2f}s)\n")
            results.append((host, outcome))

    print("Summary (one sample each, taken just now, from this machine):")
    for host, outcome in results:
        print(f"  {host:<34} {outcome[:96]}")
    print("\nA single failure is a snapshot, not a verdict: re-run in a few "
          "minutes before concluding that a mirror is down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
