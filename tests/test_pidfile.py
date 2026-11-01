"""Loop liveness: the dashboard must detect the loop without ever killing it.

On Windows ``os.kill(pid, 0)`` is TerminateProcess, so every test here
asserts os.kill is never called on that path. Nothing here signals a real
process other than the test runner itself (signal 0 on POSIX only).
"""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
import harvey.pidfile as pidfile


def _no_kill(*args, **kwargs):
    raise AssertionError("os.kill must not be used as a liveness check here")


# ── pid_alive, per platform ──


def test_windows_live_pid_uses_kernel_query(monkeypatch):
    monkeypatch.setattr(pidfile.sys, "platform", "win32")
    monkeypatch.setattr(pidfile.os, "kill", _no_kill)
    monkeypatch.setattr(pidfile, "_pid_alive_windows", lambda pid: pid == 4242)
    assert pidfile.pid_alive(4242) is True


def test_windows_stale_pid_is_not_alive(monkeypatch):
    monkeypatch.setattr(pidfile.sys, "platform", "win32")
    monkeypatch.setattr(pidfile.os, "kill", _no_kill)
    monkeypatch.setattr(pidfile, "_pid_alive_windows", lambda pid: False)
    assert pidfile.pid_alive(16240) is False


def test_posix_live_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(pidfile.sys, "platform", "linux")
    monkeypatch.setattr(pidfile.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert pidfile.pid_alive(4242) is True
    assert calls == [(4242, 0)]


@pytest.mark.parametrize("exc", [ProcessLookupError(), OSError(22, "Invalid argument")])
def test_posix_stale_pid(monkeypatch, exc):
    def _raise(pid, sig):
        raise exc
    monkeypatch.setattr(pidfile.sys, "platform", "linux")
    monkeypatch.setattr(pidfile.os, "kill", _raise)
    assert pidfile.pid_alive(16240) is False


def test_posix_other_users_process_counts_as_alive(monkeypatch):
    def _raise(pid, sig):
        raise PermissionError()
    monkeypatch.setattr(pidfile.sys, "platform", "linux")
    monkeypatch.setattr(pidfile.os, "kill", _raise)
    assert pidfile.pid_alive(1) is True


def test_nonpositive_pid_is_never_alive():
    assert pidfile.pid_alive(0) is False
    assert pidfile.pid_alive(-1) is False


@pytest.mark.skipif(sys.platform != "win32", reason="real kernel32 query")
def test_windows_real_query_on_self_and_unused_pid():
    # Querying is read-only: our own process stays alive afterwards.
    assert pidfile._pid_alive_windows(os.getpid()) is True
    assert pidfile._pid_alive_windows(0x7FFFFFF0) is False


# ── PID file lifecycle (harvey run) ──


def test_write_then_remove_own_pid(tmp_path):
    f = tmp_path / "data" / "harvey.pid"
    pidfile.write_pid_file(f)
    assert pidfile.read_pid(f) == os.getpid()
    pidfile.remove_pid_file(f)
    assert not f.exists()


def test_remove_leaves_another_loops_pid(tmp_path):
    f = tmp_path / "harvey.pid"
    f.write_text("999999")
    pidfile.remove_pid_file(f)
    assert f.read_text() == "999999"


# ── Dashboard status endpoint ──


@pytest.fixture
def status_client(monkeypatch, tmp_path):
    f = tmp_path / "harvey.pid"
    monkeypatch.setattr(dash, "PID_FILE", f)
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "harvey.db")
    monkeypatch.setattr(dash, "_harvey_process", None)
    monkeypatch.setattr(dash.os, "kill", _no_kill)
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    token = dash.token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {token}"}) as c:
        c.pid_file = f
        yield c


def test_status_running_for_cli_started_loop(status_client, monkeypatch):
    """A loop started with `harvey run` has no dashboard subprocess, only its PID file."""
    lock = status_client.pid_file.with_name("harvey.lock")
    monkeypatch.setattr(dash, "LOCK_FILE", lock)
    status_client.pid_file.write_text("4242")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: pid == 4242)
    held = pidfile.hold_loop_lock(lock)
    try:
        data = status_client.get("/api/harvey/status").json()
    finally:
        pidfile.release_loop_lock(held)
    assert data["running"] is True
    assert data["pid"] == 4242


def test_status_stale_pid_reports_offline_and_cleans_up(status_client, monkeypatch):
    status_client.pid_file.write_text("16240")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: False)
    data = status_client.get("/api/harvey/status").json()
    assert data["running"] is False
    assert not status_client.pid_file.exists()


def test_status_garbage_pid_file(status_client):
    status_client.pid_file.write_text("not a pid")
    assert status_client.get("/api/harvey/status").json()["running"] is False
    assert not status_client.pid_file.exists()


# ── The loop lock: a PID file alone can outlive the loop ──
#
# After a crash or a reboot data/harvey.pid can name a process that is not
# Harvey at all (PIDs are reused). Only the running loop holds the lock, so
# a live PID with no lock held is somebody else's process.


def test_the_lock_is_held_only_while_a_loop_holds_it(tmp_path):
    lock = tmp_path / "harvey.lock"
    assert pidfile.loop_lock_held(lock) is False
    held = pidfile.hold_loop_lock(lock)
    assert held is not None
    assert pidfile.loop_lock_held(lock) is True
    pidfile.release_loop_lock(held)
    assert pidfile.loop_lock_held(lock) is False


def test_a_second_loop_cannot_take_the_lock(tmp_path):
    lock = tmp_path / "harvey.lock"
    first = pidfile.hold_loop_lock(lock)
    try:
        assert pidfile.hold_loop_lock(lock) is None
    finally:
        pidfile.release_loop_lock(first)


def test_probing_the_lock_does_not_take_it(tmp_path):
    lock = tmp_path / "harvey.lock"
    pidfile.loop_lock_held(lock)
    held = pidfile.hold_loop_lock(lock)
    assert held is not None
    pidfile.release_loop_lock(held)


def test_a_reused_pid_is_not_reported_as_harvey(status_client, monkeypatch, tmp_path):
    """The PID is alive, but it is some other program: no loop holds the lock."""
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    status_client.pid_file.write_text("4242")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: True)
    data = status_client.get("/api/harvey/status").json()
    assert data["running"] is False
    assert not status_client.pid_file.exists()


def test_stop_never_signals_a_reused_pid(status_client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    status_client.pid_file.write_text("4242")
    monkeypatch.setattr(dash, "pid_alive", lambda pid: True)
    # dash.os.kill is _no_kill: any signal fails the test.
    assert status_client.post("/api/harvey/stop").json()["success"] is False


def test_harvey_run_refuses_when_a_loop_is_already_running(monkeypatch, tmp_path, capsys):
    import harvey.main as main_mod
    lock = tmp_path / "harvey.lock"
    monkeypatch.setattr(main_mod, "LOCK_FILE", lock)
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: False)
    ran = []
    monkeypatch.setattr(main_mod, "_run_with_signals", lambda: ran.append(1))
    held = pidfile.hold_loop_lock(lock)
    try:
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
    finally:
        pidfile.release_loop_lock(held)
    assert exc.value.code == 1
    assert ran == []
    assert "already running" in capsys.readouterr().out


# ── Dashboard Start ──


def test_start_launches_the_heartbeat_not_the_cli_help(status_client, monkeypatch, tmp_path):
    import harvey.main as main_mod
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: False)
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "harvey.log")
    launched = {}

    class FakePopen:
        pid = 4242

        def __init__(self, cmd, **kwargs):
            launched["cmd"] = cmd
            launched["env"] = kwargs.get("env")

        def poll(self):
            return None

    monkeypatch.setattr(dash.subprocess, "Popen", FakePopen)
    assert status_client.post("/api/harvey/start").json()["success"] is True
    assert launched["cmd"][1:] == ["-m", "harvey", "run"]


class _FakeChild:
    """A dashboard-started loop: exits once it has been sent SIGTERM."""
    pid = 4242

    def __init__(self):
        self.signalled = False

    def poll(self):
        return 0 if self.signalled else None


def test_start_runs_the_loop_with_no_terminal_input(status_client, monkeypatch, tmp_path):
    """The child must never read the dashboard's terminal (the setup wizard would)."""
    import harvey.main as main_mod
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: False)
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "harvey.log")
    seen = {}

    class FakePopen(_FakeChild):
        def __init__(self, cmd, **kwargs):
            super().__init__()
            seen.update(kwargs)

    monkeypatch.setattr(dash.subprocess, "Popen", FakePopen)
    assert status_client.post("/api/harvey/start").json()["success"] is True
    assert seen["stdin"] == dash.subprocess.DEVNULL


def test_start_refuses_before_setup_is_done(status_client, monkeypatch, tmp_path):
    import harvey.main as main_mod
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: True)

    def _never(*a, **k):
        raise AssertionError("started a loop that would sit in the setup wizard")
    monkeypatch.setattr(dash.subprocess, "Popen", _never)
    data = status_client.post("/api/harvey/start").json()
    assert data["success"] is False
    assert "morthy setup" in data["message"]


def test_harvey_run_without_a_terminal_does_not_start_the_wizard(monkeypatch, capsys):
    import harvey.main as main_mod
    monkeypatch.setattr(main_mod, "_needs_setup", lambda: True)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False, raising=False)

    def _wizard():
        raise AssertionError("the setup wizard ran with nobody to answer it")
    import harvey.setup
    monkeypatch.setattr(harvey.setup, "run_setup", _wizard)
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 1
    assert "harvey setup" in capsys.readouterr().out


def test_stop_that_could_not_end_the_loop_says_so(status_client, monkeypatch, tmp_path):
    """Started by another user or an elevated terminal: the signal is refused.
    Stop must not claim success, or forget the loop it failed to stop."""
    lock = tmp_path / "harvey.lock"
    monkeypatch.setattr(dash, "LOCK_FILE", lock)
    monkeypatch.setattr(dash, "pid_alive", lambda pid: True)
    monkeypatch.setattr(dash.asyncio, "sleep", lambda s: _done())

    def _refused(pid, sig):
        raise PermissionError()
    monkeypatch.setattr(dash.os, "kill", _refused)
    status_client.pid_file.write_text("4242")
    held = pidfile.hold_loop_lock(lock)
    try:
        data = status_client.post("/api/harvey/stop").json()
    finally:
        pidfile.release_loop_lock(held)
    assert data["success"] is False
    assert status_client.pid_file.read_text() == "4242"


def test_stopping_the_dashboards_own_loop_needs_no_force_kill(status_client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "LOCK_FILE", tmp_path / "harvey.lock")
    child = _FakeChild()
    monkeypatch.setattr(dash, "_harvey_process", child)
    # A zombie still answers signal 0, so pid_alive alone would say "alive".
    monkeypatch.setattr(dash, "pid_alive", lambda pid: True)
    sent = []

    def _kill(pid, sig):
        sent.append(sig)
        child.signalled = True
    monkeypatch.setattr(dash.os, "kill", _kill)
    monkeypatch.setattr(dash.asyncio, "sleep", lambda s: _done())
    assert status_client.post("/api/harvey/stop").json()["success"] is True
    assert sent == [dash.signal.SIGTERM]


async def _done():
    return None


def test_taking_the_lock_waits_out_a_status_probe(tmp_path):
    """The dashboard's status check holds the lock for an instant; a loop
    starting in that instant must not conclude another loop is running."""
    import threading
    import time
    lock = tmp_path / "harvey.lock"
    probe = pidfile.hold_loop_lock(lock)
    threading.Timer(0.03, pidfile.release_loop_lock, args=(probe,)).start()
    started = time.monotonic()
    held = pidfile.hold_loop_lock(lock)
    try:
        assert held is not None
        assert time.monotonic() - started < 1
    finally:
        pidfile.release_loop_lock(held)
