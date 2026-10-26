"""Single-instance guard for `harvey run`.

Two heartbeats against one SQLite database double-write campaigns and burn
brain quota. The scheduled task's IgnoreNew only protects while the scheduler
still tracks its own instance — on 2026-10-26 `Stop-ScheduledTask` reported
success but left the loop alive in session 0, where a normal shell cannot kill
it. So the loop itself refuses to start when an *established* one is alive.

The venv `harvey.exe` shim plus its python child look like two more instances
to a naive PID check, which is why age matters: only processes older than
MIN_OTHER_AGE_SECONDS are treated as a competing heartbeat.
"""

import os
import subprocess

import pytest

from harvey import cli


class _Result:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


def _fake_tasklist(monkeypatch, stdout):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return _Result(stdout)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _ages(monkeypatch, mapping):
    monkeypatch.setattr(cli, "_process_age_seconds", lambda pid: mapping.get(pid))


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv("HARVEY_ALLOW_MULTI", raising=False)


def test_detects_an_established_heartbeat(monkeypatch):
    _fake_tasklist(monkeypatch,
                   '"harvey.exe","4242","Console","1","123,456 K"\n')
    _ages(monkeypatch, {4242: 3600})
    assert cli._other_heartbeat_pids() == [4242]


def test_ignores_this_launchs_own_shim(monkeypatch):
    """The exe shim and its child are seconds old — never a second heartbeat."""
    _fake_tasklist(
        monkeypatch,
        '"harvey.exe","111","Console","1","1 K"\n'
        '"harvey.exe","222","Console","1","1 K"\n',
    )
    _ages(monkeypatch, {111: 1.5, 222: 2.0})
    assert cli._other_heartbeat_pids() == []


def test_ignores_the_current_process(monkeypatch):
    _fake_tasklist(
        monkeypatch,
        f'"harvey.exe","{os.getpid()}","Console","1","90,000 K"\n'
        '"harvey.exe","777","Console","1","90,000 K"\n',
    )
    _ages(monkeypatch, {os.getpid(): 99999, 777: 500})
    assert cli._other_heartbeat_pids() == [777]


def test_unreadable_age_counts_as_running(monkeypatch):
    """An orphan we cannot inspect (session 0) still blocks a second loop."""
    _fake_tasklist(monkeypatch, '"harvey.exe","4242","Console","1","1 K"\n')
    _ages(monkeypatch, {4242: None})
    assert cli._other_heartbeat_pids() == [4242]


def test_no_output_means_no_other_instance(monkeypatch):
    _fake_tasklist(monkeypatch, "INFO: No tasks are running which match the criteria.\n")
    _ages(monkeypatch, {})
    assert cli._other_heartbeat_pids() == []


def test_tasklist_failure_is_not_fatal(monkeypatch):
    def boom(args, **kwargs):
        raise OSError("tasklist missing")

    monkeypatch.setattr(subprocess, "run", boom)
    _ages(monkeypatch, {})
    assert cli._other_heartbeat_pids() == []


def test_override_env_allows_a_second_loop(monkeypatch):
    monkeypatch.setenv("HARVEY_ALLOW_MULTI", "1")
    calls = _fake_tasklist(monkeypatch, '"harvey.exe","4242","Console","1","1 K"\n')
    _ages(monkeypatch, {4242: 3600})
    assert cli._other_heartbeat_pids() == []
    assert calls == []          # never even asks


def test_cmd_run_refuses_when_one_is_alive(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_other_heartbeat_pids", lambda: [4242])
    started = []
    monkeypatch.setattr("harvey.main.main", lambda: started.append(True))

    cli.cmd_run(None)

    assert started == []
    assert "already running (pid 4242)" in capsys.readouterr().out


def test_cmd_run_starts_when_none_is_alive(monkeypatch):
    monkeypatch.setattr(cli, "_other_heartbeat_pids", lambda: [])
    started = []
    monkeypatch.setattr("harvey.main.main", lambda: started.append(True))

    cli.cmd_run(None)

    assert started == [True]
