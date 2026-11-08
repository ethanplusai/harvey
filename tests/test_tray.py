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


def test_icon_is_the_bird_with_a_state_dot():
    pytest.importorskip("PIL")
    from sellbird.desktop.tray import COLORS, make_icon

    for dark, bird in ((True, (255, 255, 255)), (False, (29, 29, 31))):
        image = make_icon("running", dark)
        assert image.size == (64, 64)
        pixels = [image.getpixel((x, y)) for x in range(64) for y in range(64)]
        # the swallow is drawn in the bar's contrasting color, not one flat circle
        assert sum(1 for p in pixels if p[3] > 200 and p[:3] == bird) > 200
        assert image.getpixel((0, 0))[3] == 0
    for state, hex_ in COLORS.items():
        dot = make_icon(state, True).getpixel((47, 47))
        want = tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))
        assert all(abs(a - b) < 12 for a, b in zip(dot[:3], want)), state


def test_menubar_icon_is_drawn_at_retina_size_and_glyph_height():
    pytest.importorskip("PIL")
    import io

    from PIL import Image

    from sellbird.desktop.tray import MENUBAR_HEIGHT_PT, menubar_png

    png, (w, h) = menubar_png("running", True)
    image = Image.open(io.BytesIO(png))
    assert h == MENUBAR_HEIGHT_PT and image.size[1] == h * 2
    assert w > h   # wider than tall, like the bird


def test_a_count_makes_the_icon_wider_with_a_bubble():
    pytest.importorskip("PIL")
    from sellbird.desktop.tray import draw_menubar

    plain, (w0, _) = draw_menubar("running", True, 0)
    three, (w3, _) = draw_menubar("running", True, 3)
    many, (w99, _) = draw_menubar("running", True, 250)
    assert w0 < w3 < w99


def test_the_app_icon_is_the_white_bird_on_harbor_blue():
    pytest.importorskip("PIL")
    from sellbird.desktop.tray import app_icon

    image = app_icon(128)
    assert image.size == (128, 128) and image.getpixel((0, 0))[3] == 0
    assert image.getpixel((20, 64))[:3] == (0x24, 0x4B, 0x62)


# ── What's waiting on you, and new replies ────────────────────────────────

from sellbird.desktop.tray import Reply, new_reply_messages, reply_message, waiting_label


def test_the_menu_says_what_waits_on_you():
    from sellbird.desktop.tray import TrayStatus, menu_items
    status = TrayStatus("running", "running", needs_you=2, to_approve=1)
    assert waiting_label(status) == "2 replies need you · 1 email to approve"
    rows = menu_items(status, False)
    assert rows[1].label == "2 replies need you · 1 email to approve" and rows[1].action == "open"
    assert waiting_label(TrayStatus("running", "running")) == ""
    assert all("need" not in r.label for r in menu_items(TrayStatus("running", "running"), False))


def test_a_reply_notification_names_who_and_quotes_them():
    r = Reply(("c1", 2), "Mike Ross", "Summit Roofing", "Sure, send it over.\nThanks")
    assert reply_message(r) == "Mike Ross at Summit Roofing replied: “Sure, send it over. Thanks”"
    yes = Reply(("c1", 2), "Mike Ross", "", "x" * 200, intent="accepted_offer")
    message = reply_message(yes)
    assert message.startswith("Mike Ross said yes to your offer:") and message.endswith("...”")


def test_only_new_replies_notify_and_a_burst_becomes_one_summary():
    seen = {("c1", 2)}
    old = Reply(("c1", 2), "A", "", "old")
    new = Reply(("c2", 3), "B", "", "new")
    assert new_reply_messages(seen, [old, new]) == [("B replied: “new”", "c2")]
    assert new_reply_messages(seen, [old, new]) == []          # never twice
    burst = [Reply((f"x{i}", 1), "C", "", "hi") for i in range(5)]
    assert new_reply_messages(seen, burst) == [("5 new replies. Open Sellbird to read them.", "")]


def test_the_first_look_sets_a_baseline_and_later_replies_pop_up():
    from sellbird.desktop.tray import TrayApp, TrayStatus

    class Controller:
        def __init__(self):
            self.rows = [Reply(("c1", 2), "A", "", "old")]

        def status(self):
            return TrayStatus("running", "running", needs_you=1)

        def replies(self):
            return list(self.rows)

    shown = []
    controller = Controller()
    app = TrayApp(controller, pystray=None, autostart=lambda: False, ui=lambda fn: fn())
    app._notify = lambda message, conversation=None: shown.append((message, conversation))
    app.refresh()
    assert shown == []                                   # nothing old pops up at start
    controller.rows.append(Reply(("c2", 1), "Mike", "Summit Roofing", "Sure, send it"))
    app.refresh()
    assert shown == [("Mike at Summit Roofing replied: “Sure, send it”", "c2")]


# ── Branded notifications through the Sellbird helper app ────────────────

def test_without_swift_or_a_prebuilt_binary_the_helper_is_not_built(tmp_path, monkeypatch):
    import shutil as sh
    from sellbird.desktop import tray
    monkeypatch.setattr(sh, "which", lambda name: None)
    monkeypatch.setattr(tray, "prebuilt_notifier", lambda: None)
    assert tray.build_notifier(tmp_path / "Sellbird.app") is False
    assert tray.MacNotifier.create(tmp_path, app=tmp_path / "Sellbird.app") is False


def test_the_helper_is_built_once_per_version(tmp_path, monkeypatch):
    from pathlib import Path
    pytest.importorskip("PIL")
    import shutil as sh
    from sellbird.desktop import tray
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "/usr/bin/swiftc":
            Path(cmd[3]).write_text("binary")
        if cmd[0] == "iconutil":
            Path(cmd[5]).write_text("icns")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(sh, "which", lambda name: "/usr/bin/swiftc")
    monkeypatch.setattr(tray, "prebuilt_notifier", lambda: None)
    app = tmp_path / "Sellbird.app"
    assert tray.build_notifier(app, run=run)
    plist = (app / "Contents" / "Info.plist").read_text()
    assert "ai.sellbird.notifier" in plist and "<string>Sellbird</string>" in plist
    assert (app / "Contents" / "MacOS" / "sellbird-notify").is_file()
    built = len(calls)
    assert tray.build_notifier(app, run=run) and len(calls) == built   # same version: no rebuild


def test_the_dashboard_link_goes_in_a_private_file_not_on_the_command_line(tmp_path, monkeypatch):
    import subprocess as sp
    from sellbird.desktop.tray import MacNotifier
    launched = []
    monkeypatch.setattr(sp, "Popen", lambda args, **kw: launched.append(args))
    notifier = MacNotifier(tmp_path / "Sellbird.app", tmp_path / "notify-link")
    assert notifier.notify("Mike replied", "http://127.0.0.1:5555/#token=secret")
    assert all("secret" not in a for a in launched[0])
    assert (tmp_path / "notify-link").read_text().endswith("#token=secret")
    assert (tmp_path / "notify-link").stat().st_mode & 0o777 == 0o600


def test_a_reply_notification_opens_that_conversation(tmp_path):
    from sellbird.desktop.tray import TrayApp, TrayStatus

    class Controller:
        data_dir = tmp_path

        def status(self):
            return TrayStatus("running", "running")

        def dashboard_link(self):
            return "http://127.0.0.1:5555/#token=t"

    class Mac:
        def __init__(self):
            self.links = []

        def notify(self, message, link):
            self.links.append(link)
            return True

    app = TrayApp(Controller(), pystray=None, autostart=lambda: False, ui=lambda fn: fn())
    app._mac = Mac()
    app._notify("Mike replied", conversation="c 2")
    app._notify("5 new replies", conversation="")
    app._notify("Sellbird started.")
    assert app._mac.links == ["http://127.0.0.1:5555/#token=t&tab=conversations&focus=c%202",
                              "http://127.0.0.1:5555/#token=t&tab=conversations",
                              "http://127.0.0.1:5555/#token=t"]


def test_the_shipped_notifier_matches_its_source():
    # Rebuild sellbird/desktop/notifier/sellbird-notify (see docs/menu-bar.md)
    # whenever main.swift changes, or every Mac without Xcode loses branding.
    from sellbird.desktop.tray import prebuilt_notifier
    assert prebuilt_notifier() is not None


def test_a_mac_without_swift_uses_the_shipped_notifier(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    import shutil as sh
    from sellbird.desktop import tray
    monkeypatch.setattr(sh, "which", lambda name: None)
    ran = []

    def run(cmd, **kw):
        ran.append(cmd[0])
        if cmd[0] == "iconutil":
            from pathlib import Path
            Path(cmd[5]).write_text("icns")
        return type("R", (), {"returncode": 0})()

    app = tmp_path / "Sellbird.app"
    assert tray.build_notifier(app, run=run)
    binary = app / "Contents" / "MacOS" / "sellbird-notify"
    assert binary.read_bytes() == tray.NOTIFIER_PREBUILT.read_bytes()
    assert "swiftc" not in " ".join(ran)
