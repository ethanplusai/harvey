"""Deterministic soak and fault injection through the real Brain and Codex
adapter against the scripted CLI (tests/fake_codex.py).

Many calls from concurrent clients, mixed with every failure class Sellbird
must survive: crashes, partial/malformed/oversized output, hangs, attempted
tool calls, usage limits, auth/isolation/version denial and cancellation.
Afterwards: every call ended within its bound, no runtime process or temp
folder is left, the native login is byte-for-byte unchanged, and only
successful calls were counted.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

import sellbird.brain as brain_mod
import sellbird.codex as hc

# The Codex provider supports macOS and Linux only (see sellbird.codex).
pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="the Codex provider isn't supported on Windows")
from sellbird.brain import Brain
from sellbird.config import BrainConfig
from sellbird.state import StateManager
from tests.test_codex import _write_login

FAKE = str(Path(__file__).with_name("fake_codex.py"))
# A call's deadline includes waiting for its Brain's runtime lock, and the
# mixed soak queues 8 calls per Brain. Healthy runtime start-ups take ~0.1 s
# each natively but seconds under CPU emulation (x86_64 on arm64 measured:
# a 4 s deadline expired while still queued). 30 s keeps the test about
# bounding and cleanup, not host speed. It doesn't slow fast hosts: no
# healthy or fault call here waits for its deadline except the dedicated
# hang job (2 s). Short deadlines (lock wait, blocked write, timeout) have
# their own tests in test_codex.py.
CALL_TIMEOUT = 30.0
# One call may take its timeout plus bounded teardown (kill + exit wait).
CALL_BOUND = CALL_TIMEOUT + hc.KILL_WAIT_SECONDS + 1.0

TURN_FAULTS = ["crash_turn", "partial", "malformed", "oversized", "toolcall"]


@pytest.fixture
def soak(monkeypatch, tmp_path):
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(runtimes))
    monkeypatch.setattr(brain_mod, "RETRY_BASE_DELAY", 0)
    native = tmp_path / "native"
    native.mkdir()
    _write_login(native)
    logs = tmp_path / "logs"
    logs.mkdir()
    real_exec = asyncio.create_subprocess_exec

    async def exec_routed(program, *args, **kwargs):
        # "codex@<scenario>" selects the fake's scenario per client.
        assert program.startswith("codex@"), program
        scenario = program.split("@", 1)[1]
        return await real_exec(sys.executable, FAKE, scenario, str(logs / f"{scenario}.log"),
                               *args, **kwargs)

    monkeypatch.setattr(hc.asyncio, "create_subprocess_exec", exec_routed)

    class Soak:
        def __init__(self):
            self.native = native
            self.runtimes = runtimes

        def entries(self, scenario=None):
            files = [logs / f"{scenario}.log"] if scenario else sorted(logs.glob("*.log"))
            out = []
            for f in files:
                if f.exists():
                    out += [json.loads(line) for line in f.read_text().splitlines()]
            return out

        def pids(self):
            return [e["pid"] for e in self.entries() if e.get("cmd") == "app-server"]

        def brain(self, state, scenario):
            brain = Brain(state, BrainConfig(provider="codex", model="gpt-5.5"))
            brain.codex._codex_path = f"codex@{scenario}"
            brain.codex.native_home = native
            return brain

        def client(self, scenario):
            return hc.CodexClient(model="gpt-5.5", codex_path=f"codex@{scenario}",
                                  native_home=native)

    return Soak()


def _native_identity(home: Path):
    st = (home / "auth.json").stat()
    return ((home / "auth.json").read_bytes(), st.st_ino, st.st_mode, st.st_mtime_ns)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


async def _timed(coro):
    started = time.monotonic()
    try:
        return await coro, time.monotonic() - started
    except BaseException as e:  # noqa: BLE001 - recorded for assertions
        return e, time.monotonic() - started


@pytest.mark.asyncio
async def test_soak_mixed_faults_across_concurrent_brains(soak, tmp_path):
    state = StateManager(str(tmp_path / "soak.db"))
    await state.init_db()
    before = _native_identity(soak.native)

    healthy = soak.brain(state, "smart")
    faulty = soak.brain(state, "smart")         # healthy CLI, faults per prompt
    isolation = soak.brain(state, "hooks")      # persistent: unsafe config layer
    auth = soak.brain(state, "logged_out")      # persistent: no ChatGPT account
    version = soak.brain(state, "old_version")  # persistent: unverified CLI
    limited = soak.brain(state, "smart")
    # Its own Brain: the tool-call fault above (correctly) pauses `faulty`.
    hanger = soak.brain(state, "smart")

    rounds = 8
    jobs = []
    for i in range(rounds):
        jobs.append(("ok", healthy.think(f"Classify this email reply #{i}: pricing?",
                                         timeout=CALL_TIMEOUT, max_retries=1)))
        fault = TURN_FAULTS[i % len(TURN_FAULTS)]
        jobs.append((fault, faulty.think(f"FAULT:{fault} #{i}", timeout=CALL_TIMEOUT,
                                         max_retries=1)))
        jobs.append(("isolation", isolation.think(f"q{i}", timeout=CALL_TIMEOUT)))
        jobs.append(("auth", auth.think(f"q{i}", timeout=CALL_TIMEOUT)))
        jobs.append(("version", version.think(f"q{i}", timeout=CALL_TIMEOUT)))
        jobs.append(("limit", limited.think("FAULT:usage_limit" if i == 0 else f"q{i}",
                                            timeout=CALL_TIMEOUT)))
    # One hang, bounded by its own deadline.
    jobs.append(("hang", hanger.think("FAULT:hang", timeout=2.0, max_retries=0)))

    started = time.monotonic()
    results = await asyncio.gather(*(_timed(c) for _, c in jobs))
    elapsed = time.monotonic() - started

    outcomes = {}
    for (kind, _), (value, seconds) in zip(jobs, results):
        assert not isinstance(value, BaseException), (kind, value)
        # Retries double the worst case for retried kinds.
        assert seconds < 2 * CALL_BOUND, (kind, seconds)
        outcomes.setdefault(kind, []).append(value)
        if kind == "hang":
            # Really hung, and was cut off by its own 2 s deadline.
            assert 2.0 <= seconds < 2.0 + hc.KILL_WAIT_SECONDS + 1.0, seconds

    assert outcomes["ok"] == ["interested"] * rounds
    for kind in TURN_FAULTS + ["isolation", "auth", "version", "limit", "hang"]:
        assert all(v == "" for v in outcomes[kind]), kind
    assert elapsed < rounds * 3 * CALL_BOUND  # serialized per brain, bounded overall

    # Persistent failures: one runtime attempt, then no more launches.
    assert isolation.unavailable and auth.unavailable and version.unavailable
    assert sum(1 for e in soak.entries("hooks") if e.get("method") == "hooks/list") == 1
    assert sum(1 for e in soak.entries("logged_out") if e.get("method") == "account/read") == 1
    assert sum(1 for e in soak.entries("old_version") if e.get("cmd") == "--version") == 1
    # Usage limit: the first turn was refused, the rest never started.
    assert limited.paused_reason

    # Nothing left behind, login untouched, only successes counted.
    if os.name == "posix":
        assert not [p for p in soak.pids() if _alive(p)]
    assert list(soak.runtimes.iterdir()) == []
    assert _native_identity(soak.native) == before
    assert await state.get_usage_today("codex") == rounds
    rows = await state.usage_by_provider(days=1)
    assert [(r["provider"], r["calls"]) for r in rows] == [("codex", rounds)]


@pytest.mark.asyncio
async def test_soak_many_concurrent_clients(soak):
    """Six independent clients, five calls each, all at once."""
    clients = [soak.client("smart") for _ in range(6)]
    before = _native_identity(soak.native)
    started = time.monotonic()
    results = await asyncio.gather(*(
        c.run(f"Classify this email reply {i}-{n}: pricing?", timeout=30)
        for i, c in enumerate(clients) for n in range(5)))
    elapsed = time.monotonic() - started
    assert [r.text for r in results] == ["interested"] * 30
    assert len({r.thread_id for r in results}) == 30  # one runtime per call
    assert elapsed < 120
    if os.name == "posix":
        assert not [p for p in soak.pids() if _alive(p)]
    assert list(soak.runtimes.iterdir()) == []
    assert _native_identity(soak.native) == before


@pytest.mark.asyncio
async def test_soak_cancellation_storm(soak):
    """Cancel calls at many different points; nothing may leak."""
    client = soak.client("smart")
    before = _native_identity(soak.native)
    for delay in (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2):
        task = asyncio.ensure_future(client.run("FAULT:hang", timeout=30))
        await asyncio.sleep(delay)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The client still works after all that.
    assert (await client.run("Classify this email reply: pricing?", timeout=30)).text == "interested"
    if os.name == "posix":
        assert not [p for p in soak.pids() if _alive(p)]
    assert list(soak.runtimes.iterdir()) == []
    assert _native_identity(soak.native) == before
