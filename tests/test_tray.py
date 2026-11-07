"""The menu-bar app: a client of the dashboard, with the network and OS faked."""

import argparse
import builtins
import json

import httpx
import pytest

from sellbird import dashboard_instance
from sellbird.desktop import tray
from sellbird.desktop_cli import cmd_tray


class FakeDashboard:
    def __init__(self, instance="abc", token="user-token"):
        self.instance = instance
        self.token = token
        self.running = False
        self.paused = ""
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/instance":
            return httpx.Response(200, json={"instance": self.instance})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if path == "/api/sellbird/status":
            return httpx.Response(200, json={"running": self.running, "sending_paused": self.paused})
        if path == "/api/sellbird/start":
            self.running = True
            return httpx.Response(200, json={"success": True})
        if path == "/api/sellbird/stop":
            self.running = False
            return httpx.Response(200, json={"success": True})
        if path == "/api/sending/pause":
            self.paused = "paused from dashboard"
            return httpx.Response(200, json={"success": True})
        if path == "/api/sending/resume":
            self.paused = ""
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={})


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    from sellbird.dashboard_auth import load_or_create_token
    fake = FakeDashboard(token=load_or_create_token(data / "dashboard-token"))
    opened, launched = [], []
    controller = tray.TrayController(
        data, port=5600, transport=httpx.MockTransport(fake.handler),
        launcher=lambda argv, log: launched.append((argv, log)),
        opener=opened.append, sleep=lambda s: None)
    return data, fake, controller, opened, launched


def _publish(data, instance="abc"):
    dashboard_instance.write(data, host="127.0.0.1", port=5600, instance=instance)


def test_no_dashboard_means_stopped_and_nothing_is_sent(setup):
    data, fake, c, opened, _ = setup
    assert c.status().dashboard == "stopped"
    assert fake.requests == []
    assert c.open_dashboard() is False and opened == []
    assert c.start_loop() == tray.NOT_RUNNING


def test_another_program_on_the_port_never_gets_the_token(setup):
    data, fake, c, _, _ = setup
    _publish(data, instance="someone-else")
    assert c.status().dashboard == "stopped"
    assert all("authorization" not in r.headers for r in fake.requests)


def test_it_starts_the_dashboard_and_waits_for_it(setup, monkeypatch):
    data, fake, c, _, launched = setup
    clock = iter(range(1000))
    monkeypatch.setattr(c, "_clock", lambda: next(clock))
    c._launcher = lambda argv, log: (launched.append((argv, log)), _publish(data))
    assert c.start_dashboard() == "The dashboard started."
    argv, log = launched[0]
    assert argv[-4:] == ["sellbird", "dashboard", "--port", "5600"] and "-P" in argv
    assert log == data / tray.START_LOG
    assert c.start_dashboard() == "The dashboard is running."
    assert len(launched) == 1


def test_it_says_so_when_the_dashboard_never_answers(setup, monkeypatch):
    data, fake, c, _, launched = setup
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(c, "_clock", lambda: next(clock))
    assert "didn't answer" in c.start_dashboard()


def test_the_menu_follows_the_loop_and_sending(setup):
    data, fake, c, _, _ = setup
    _publish(data)
    status = c.status()
    assert (status.dashboard, status.loop) == ("running", "stopped")
    labels = [r.label for r in tray.menu_items(status, False)]
    assert "Start Sellbird" in labels and "Pause sending" in labels

    assert c.start_loop() == "Sellbird started."
    assert c.pause_sending() == "Sending is paused."
    status = c.status()
    assert tray.state_label(status) == "running, sending paused"
    assert tray.icon_state(status) == "paused"
    labels = [r.label for r in tray.menu_items(status, True)]
    assert "Stop Sellbird" in labels and "Resume sending" in labels
    assert c.resume_sending() == "Sending resumed." and c.stop_loop() == "Sellbird stopped."
    posts = [r for r in fake.requests if r.method == "POST"]
    assert all(r.headers["authorization"] == f"Bearer {fake.token}" for r in posts)


def test_a_rejected_token_reads_as_signed_out(setup):
    data, fake, c, _, _ = setup
    _publish(data)
    fake.token = "a-new-one"
    status = c.status()
    assert status.error == tray.SIGNED_OUT and tray.state_label(status) == "signed out"
    rows = {r.action: r for r in tray.menu_items(status, False)}
    assert not rows["start_loop"].enabled and rows["open"].enabled


def test_open_hands_the_link_to_the_browser_only(setup, capsys):
    data, fake, c, opened, _ = setup
    _publish(data)
    assert c.open_dashboard() is True
    assert opened == [f"http://127.0.0.1:5600/#token={fake.token}"]
    assert fake.token not in capsys.readouterr().out


def test_the_port_is_remembered(tmp_path):
    assert tray.resolve_port(tmp_path, None) == tray.DEFAULT_PORT
    tray.remember_port(tmp_path, 5601)
    assert tray.resolve_port(tmp_path, None) == 5601
    assert tray.resolve_port(tmp_path, 5602) == 5602
    (tmp_path / tray.TRAY_JSON).write_text(json.dumps({"port": "x"}))
    assert tray.resolve_port(tmp_path, None) == tray.DEFAULT_PORT


def test_actions_run_off_the_menu_thread_and_refresh(setup):
    data, fake, c, _, _ = setup
    _publish(data)
    ran = []

    class Py:
        class MenuItem:
            def __init__(self, label, action, checked=None, enabled=True):
                self.label, self.action, self.enabled = label, action, enabled

    app = tray.TrayApp(c, Py, autostart=lambda: False, spawn=lambda fn: (ran.append(fn), fn()),
                       ui=lambda fn: None)
    app.refresh()
    items = app.menu_items()
    start = next(i for i in items if i.label == "Start Sellbird")
    start.action(None, start)
    assert fake.running and ran
    assert any(i.label == "Stop Sellbird" for i in app.menu_items())


def test_no_tray_without_pystray(tmp_path, monkeypatch):
    real = builtins.__import__

    def blocked(name, *a, **k):
        if name == "pystray":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(tray.TrayUnavailable):
        tray.run_tray(tmp_path)


def test_status_prints_no_link_or_token(setup, capsys):
    data, fake, c, _, _ = setup
    _publish(data)
    cmd_tray(argparse.Namespace(status=True, port=5600), data_dir=data,
             transport=httpx.MockTransport(fake.handler))
    out = capsys.readouterr().out
    assert "Sellbird: stopped" in out and fake.token not in out and "http" not in out


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX modes")
def test_the_start_log_is_owner_only(tmp_path, monkeypatch):
    import os
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    log = tmp_path / "data" / tray.START_LOG
    tray.default_launcher(["true"], log)
    assert os.stat(log).st_mode & 0o777 == 0o600
