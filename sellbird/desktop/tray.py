"""The menu-bar (macOS) and tray (Windows) icon: start, stop and open Sellbird.

The tray is a client of the dashboard, never a second one. It finds the
dashboard the way the MCP server does (data/dashboard.json, then
/api/instance must answer with the same id), and starts `sellbird dashboard`
as a detached process when there is none, so closing the tray leaves it
running. Everything else goes through the dashboard's own routes with the
user's token: starting and stopping the loop, pausing and resuming sending.

The unlock link carries the dashboard token in its #fragment. It goes to the
OS browser opener only; this module never prints or logs it.

pystray and Pillow are imported inside functions: they're installed on macOS
and Windows only, and Linux runs Sellbird without a tray.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import httpx

from sellbird import dashboard_instance

logger = logging.getLogger("sellbird.tray")

DEFAULT_PORT = 5555
TRAY_LOCK = "tray.lock"
TRAY_JSON = "tray.json"
START_LOG = "dashboard-start.log"
REFRESH_SECONDS = 5.0
START_WAIT_SECONDS = 20.0

COLORS = {"running": "#2e9e5b", "paused": "#c9a227", "stopped": "#8a8f98"}

NOT_RUNNING = "The dashboard isn't running."
UNREACHABLE = "Couldn't reach the dashboard."
SIGNED_OUT = "The dashboard didn't accept the saved token. Open it from the menu."


class TrayUnavailable(Exception):
    """No tray on this platform (pystray isn't installed)."""


@dataclass(frozen=True)
class TrayStatus:
    dashboard: Literal["running", "stopped", "starting"]
    loop: Literal["running", "stopped", "unknown"]
    sending_paused: str = ""
    error: str = ""


@dataclass(frozen=True)
class MenuRow:
    label: str
    action: str | None
    enabled: bool
    checked: bool = False


# ── The port ──

def resolve_port(data_dir: Path, port: int | None) -> int:
    """`--port` when given, else the port the tray was last told, else 5555."""
    if port is not None:
        return port
    try:
        saved = json.loads((Path(data_dir) / TRAY_JSON).read_text(encoding="utf-8"))["port"]
        if isinstance(saved, int) and 1 <= saved <= 65535:
            return saved
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return DEFAULT_PORT


def remember_port(data_dir: Path, port: int) -> None:
    """So the login item's `sellbird tray` (no --port) reuses it."""
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    (Path(data_dir) / TRAY_JSON).write_text(json.dumps({"port": port}), encoding="utf-8")


# ── Starting the dashboard ──

def dashboard_command(port: int, platform: str = sys.platform) -> list[str]:
    """`python -P -m sellbird dashboard --port N`. On Windows, pythonw.exe so no
    console window opens. -P keeps the folder the tray started in off
    sys.path, so a Sellbird checkout there can't be run instead."""
    python = sys.executable
    if platform == "win32":
        pythonw = Path(python).with_name("pythonw.exe")
        if pythonw.exists():
            python = str(pythonw)
    return [python, "-P", "-m", "sellbird", "dashboard", "--port", str(port)]


def default_launcher(argv: list[str], log_path: Path) -> None:
    """Start the dashboard detached from this process (and its terminal), its
    output appended to log_path. The dashboard prints its unlock link, so
    that log is owner-only, like data/dashboard-token."""
    import os
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as log:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)


# ── Talking to the dashboard ──

def _read_user_token(data_dir: Path) -> str:
    """The user's dashboard token. The tray is the user's own app: it sends
    this to the dashboard it verified and to nobody else, and never shows it."""
    from sellbird.dashboard_auth import TokenError, TokenStore
    try:
        # Never creates one: the dashboard does, when it starts.
        return TokenStore(Path(data_dir) / "dashboard-token").peek() or ""
    except (TokenError, OSError):
        return ""


class TrayController:
    """Everything the menu does, with the OS and the network injected."""

    def __init__(self, data_dir: Path, port: int = DEFAULT_PORT, *,
                 transport: httpx.BaseTransport | None = None,
                 launcher: Callable[[list[str], Path], None] = default_launcher,
                 opener: Callable[[str], object] = webbrowser.open,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.data_dir = Path(data_dir)
        self.port = port
        self._transport = transport
        self._launcher = launcher
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._starting_until = 0.0

    def _client(self, base: str) -> httpx.Client:
        extra = {"transport": self._transport} if self._transport is not None else {}
        return httpx.Client(base_url=base, timeout=5.0, **extra)

    def _base(self) -> str | None:
        """The running dashboard's address, once its instance id matches."""
        from sellbird.mcp_server import base_url
        info = dashboard_instance.read(self.data_dir)
        if info is None:
            return None
        base = base_url(info.host, info.port)
        try:
            with self._client(base) as http:
                answer = http.get("/api/instance")
            if answer.status_code == 200 and answer.json().get("instance") == info.instance:
                return base
        except (httpx.HTTPError, OSError, ValueError, AttributeError):
            pass
        return None

    def _call(self, method: str, path: str) -> tuple[int, dict]:
        base = self._base()
        if base is None:
            return 0, {"message": NOT_RUNNING}
        token = _read_user_token(self.data_dir)
        try:
            with self._client(base) as http:
                r = http.request(method, path, headers={"Authorization": f"Bearer {token}"})
        except (httpx.HTTPError, OSError):
            return 0, {"message": UNREACHABLE}
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body if isinstance(body, dict) else {}

    def status(self) -> TrayStatus:
        code, loop = self._call("GET", "/api/sellbird/status")
        if code == 0:
            starting = self._clock() < self._starting_until
            return TrayStatus("starting" if starting else "stopped", "unknown")
        if code in (401, 403):
            return TrayStatus("running", "unknown", error=SIGNED_OUT)
        return TrayStatus("running", "running" if loop.get("running") else "stopped",
                          sending_paused=loop.get("sending_paused") or "")

    def start_dashboard(self) -> str:
        """Start the dashboard unless one is up, and wait a little for it."""
        if self._base() is not None:
            return "The dashboard is running."
        self._launcher(dashboard_command(self.port), self.data_dir / START_LOG)
        self._starting_until = self._clock() + START_WAIT_SECONDS
        while self._clock() < self._starting_until:
            if self._base() is not None:
                self._starting_until = 0.0
                return "The dashboard started."
            self._sleep(0.5)
        return f"The dashboard didn't answer yet. See {self.data_dir / START_LOG}."

    def _post(self, path: str, done: str) -> str:
        code, body = self._call("POST", path)
        if code == 200 and body.get("success", True) is not False:
            return done
        return body.get("message") or UNREACHABLE

    def start_loop(self) -> str:
        return self._post("/api/sellbird/start", "Sellbird started.")

    def stop_loop(self) -> str:
        return self._post("/api/sellbird/stop", "Sellbird stopped.")

    def pause_sending(self) -> str:
        return self._post("/api/sending/pause", "Sending is paused.")

    def resume_sending(self) -> str:
        return self._post("/api/sending/resume", "Sending resumed.")

    def open_dashboard(self) -> bool:
        info = dashboard_instance.read(self.data_dir)
        if info is None or self._base() is None:
            return False
        from sellbird.mcp_server import base_url
        token = _read_user_token(self.data_dir)
        self._opener(base_url(info.host, info.port) + (f"#token={token}" if token else ""))
        return True


# ── The menu ──

def state_label(status: TrayStatus) -> str:
    if status.dashboard == "starting":
        return "starting…"
    if status.dashboard != "running":
        return "not running"
    if status.error:
        return "signed out"
    if status.loop != "running":
        return "stopped"
    return "running, sending paused" if status.sending_paused else "running"


def icon_state(status: TrayStatus) -> str:
    if status.dashboard != "running" or status.loop != "running":
        return "stopped" if status.dashboard != "running" else "paused"
    return "paused" if status.sending_paused else "running"


def menu_items(status: TrayStatus, autostart_on: bool) -> list[MenuRow]:
    up = status.dashboard == "running" and not status.error
    rows = [MenuRow(f"Sellbird: {state_label(status)}", None, False),
            MenuRow("Open dashboard", "open", status.dashboard == "running")]
    if status.loop == "running":
        rows.append(MenuRow("Stop Sellbird", "stop_loop", up))
    else:
        rows.append(MenuRow("Start Sellbird", "start_loop", up))
    if status.sending_paused:
        rows.append(MenuRow("Resume sending", "resume_sending", up))
    else:
        rows.append(MenuRow("Pause sending", "pause_sending", up))
    if status.dashboard == "stopped":
        rows.append(MenuRow("Start the dashboard", "start_dashboard", True))
    rows.append(MenuRow("Start at login", "toggle_autostart", True, checked=autostart_on))
    rows.append(MenuRow("Quit (Sellbird keeps running)" if up else "Quit", "quit", True))
    return rows


# The Sellbird swallow, from sellbird/web/sellbird-mark.svg (viewBox 0 0 1040 640).
BIRD_PATH = ("M83 0C246 47 417 91 562 157C676 209 759 258 744 314C813 257 896 239 954 301"
             "L1034 324L944 359C837 473 731 548 563 524C506 516 464 513 427 519L0 631L407 406"
             "L224 213L555 300L420 428L663 315L630 281C407 229 264 237 83 0Z")
BIRD_BOX = (1040, 640)


def bird_points(width: float, height: float, x0: float = 0, y0: float = 0,
                steps: int = 16) -> list[tuple[float, float]]:
    """BIRD_PATH as a polygon scaled into a width x height box at (x0, y0).

    The path only uses absolute M, L, C and Z, so curves are sampled here
    instead of pulling in an SVG renderer."""
    import re

    tokens = re.findall(r"[MLCZ]|-?\d+(?:\.\d+)?", BIRD_PATH)
    sx, sy = width / BIRD_BOX[0], height / BIRD_BOX[1]
    pts: list[tuple[float, float]] = []
    cur = (0.0, 0.0)
    i, cmd = 0, ""
    while i < len(tokens):
        if tokens[i] in "MLCZ":
            cmd = tokens[i]
            i += 1
            if cmd == "Z":
                continue
        nums = lambda n: [float(t) for t in tokens[i:i + n]]  # noqa: E731
        if cmd in "ML":
            cur = tuple(nums(2))
            pts.append(cur)
            i += 2
        elif cmd == "C":
            x1, y1, x2, y2, x3, y3 = nums(6)
            x0_, y0_ = cur
            for k in range(1, steps + 1):
                t = k / steps
                u = 1 - t
                pts.append((u**3 * x0_ + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                            u**3 * y0_ + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3))
            cur = (x3, y3)
            i += 6
        else:
            i += 1
    return [(x0 + x * sx, y0 + y * sy) for x, y in pts]


def _dark_bar() -> bool:
    """Whether the bar behind the icon is dark: macOS dark mode, and Windows' taskbar."""
    if sys.platform != "darwin":
        return True
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().lower() == "dark"


def make_icon(state: str, dark: bool | None = None):
    """64×64: the Sellbird swallow, with a dot in the state's color at the bottom right."""
    from PIL import Image, ImageDraw

    if dark is None:
        dark = _dark_bar()
    scale = 4  # drawn large, then shrunk, for smooth edges
    size = 64 * scale
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    bird_w = 62 * scale
    bird_h = bird_w * BIRD_BOX[1] / BIRD_BOX[0]
    top = (size - bird_h) / 2
    bird = "#ffffff" if dark else "#1d1d1f"
    draw.polygon(bird_points(bird_w, bird_h, 1 * scale, top), fill=bird)
    r = 8 * scale
    cx, cy = size - r - 2 * scale, size - r - 8 * scale
    ring = 3 * scale
    # clear a ring around the dot so it reads on top of the wing
    hole = Image.new("L", (size, size), 0)
    ImageDraw.Draw(hole).ellipse((cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring), fill=255)
    image.paste((0, 0, 0, 0), mask=hole)
    draw = ImageDraw.Draw(image)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=COLORS.get(state, COLORS["stopped"]))
    return image.resize((64, 64), Image.LANCZOS)


# ── The tray app ──

def _autostart_on() -> bool:
    from sellbird.desktop import login_item
    try:
        state = login_item.status()
    except Exception as e:  # an unreadable item shows as off
        logger.debug("Start at login unreadable: %s", e)
        return False
    return bool(state["installed"] and state["ours"])


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def main_thread_scheduler(platform: str = sys.platform) -> Callable[[Callable[[], None]], None]:
    """AppKit may only be touched from the main thread, so on macOS UI updates
    are queued onto it; pystray's other backends are thread-safe."""
    if platform == "darwin":
        try:
            from PyObjCTools.AppHelper import callAfter
            return callAfter
        except ImportError:
            pass
    return lambda fn: fn()


_ACTIONS = ("start_loop", "stop_loop", "pause_sending", "resume_sending", "start_dashboard")


class TrayApp:
    """The menu and its actions. Actions run off the menu's thread, then the
    menu is refreshed."""

    def __init__(self, controller: TrayController, pystray, *,
                 autostart: Callable[[], bool] | None = None,
                 spawn: Callable[[Callable[[], None]], None] = _spawn,
                 ui: Callable[[Callable[[], None]], None] | None = None):
        self.controller = controller
        self.pystray = pystray
        self._autostart = autostart if autostart is not None else (lambda: _autostart_on())
        self._spawn = spawn
        self._ui = ui if ui is not None else main_thread_scheduler()
        self.status = TrayStatus("stopped", "unknown")
        self.autostart_on = False
        self.icon = None
        self._drawn: tuple[str, bool] | None = None
        self._lock = threading.Lock()

    def refresh(self, *, autostart: bool = False) -> None:
        status = self.controller.status()
        autostart_on = self._autostart() if autostart else None
        with self._lock:
            self.status = status
            if autostart_on is not None:
                self.autostart_on = autostart_on
        self._ui(lambda: self._apply(status))

    def _apply(self, status: TrayStatus) -> None:
        if self.icon is not None:
            look = (icon_state(status), _dark_bar())
            if look != self._drawn:  # redraw only when the state or the bar's color changes
                self.icon.icon = make_icon(*look)
                self._drawn = look
            self.icon.update_menu()

    def _notify(self, message: str) -> None:
        def show():
            if self.icon is not None:
                try:
                    self.icon.notify(message, "Sellbird")
                except Exception:
                    logger.debug("Notification not shown")
        self._ui(show)

    def menu_items(self) -> list:
        with self._lock:
            rows = menu_items(self.status, self.autostart_on)
        items = []
        for row in rows:
            action = None if row.action is None else self._handler(row.action)
            checked = (lambda item, on=row.checked: on) if row.action == "toggle_autostart" else None
            items.append(self.pystray.MenuItem(row.label, action, checked=checked,
                                               enabled=row.enabled))
        return items

    def _handler(self, action: str):
        def handle(icon, item):
            self.dispatch(action)
        return handle

    def dispatch(self, action: str) -> None:
        if action == "quit":
            if self.icon is not None:
                self.icon.stop()
            return

        def work():
            if action == "toggle_autostart":
                message, ok = self._toggle_autostart()
                if not ok:
                    self._notify(message)
            elif action == "open":
                message = "" if self.controller.open_dashboard() else NOT_RUNNING
            elif action in _ACTIONS:
                message = getattr(self.controller, action)()
            else:
                message = ""
            if message:
                logger.info("%s", message)
            self.refresh(autostart=action == "toggle_autostart")

        self._spawn(work)

    def _toggle_autostart(self) -> tuple[str, bool]:
        from sellbird.desktop import login_item as li
        try:
            state = li.status()
            if state["installed"] and state["ours"]:
                li.uninstall()
                return "Start at login is off.", True
            port = self.controller.port
            li.install(port=port if port not in (None, DEFAULT_PORT) else None)
            return "Start at login is on.", True
        except Exception as e:  # the menu stays up
            return str(e) or "Start at login didn't change.", False


def _hide_dock_icon() -> None:
    """macOS: an accessory app, so the menu bar has the icon and the Dock doesn't."""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(1)
    except Exception:
        logger.debug("Could not hide the Dock icon")


def run_tray(data_dir: Path, port: int | None = None, *,
             controller: TrayController | None = None, platform: str = sys.platform,
             autostart: Callable[[], bool] | None = None,
             refresh_every: float | None = REFRESH_SECONDS,
             ui: Callable[[Callable[[], None]], None] | None = None) -> None:
    """Show the icon until Quit. Starts the dashboard when it isn't running.

    A second tray on the same data folder returns at once (exit 0), so the
    login item's KeepAlive never relaunches in a loop.
    """
    try:
        import pystray
    except ImportError:
        raise TrayUnavailable(
            "The menu-bar icon runs on macOS and Windows. On Linux, run "
            "`sellbird dashboard` and `sellbird run` in a terminal or as services.") from None

    from sellbird.pidfile import hold_loop_lock, release_loop_lock

    data_dir = Path(data_dir)
    lock = hold_loop_lock(data_dir / TRAY_LOCK)
    if lock is None:
        logger.debug("Another tray is running for this folder.")
        return
    try:
        if platform == "darwin":
            _hide_dock_icon()
        if controller is None:
            controller = TrayController(data_dir, port=resolve_port(data_dir, port))
        app = TrayApp(controller, pystray, autostart=autostart,
                      ui=ui if ui is not None else main_thread_scheduler(platform))
        stop = threading.Event()

        def refresher():
            while not stop.wait(refresh_every):
                try:
                    app.refresh()
                except Exception as e:  # keep the menu alive
                    logger.debug("Refresh failed: %s", type(e).__name__)

        def setup(icon):
            icon.visible = True
            app.refresh(autostart=True)
            logger.info("%s", controller.start_dashboard())
            app.refresh()
            if refresh_every:
                threading.Thread(target=refresher, daemon=True).start()

        icon = pystray.Icon("sellbird", icon=make_icon("stopped"), title="Sellbird",
                            menu=pystray.Menu(app.menu_items))
        app.icon = icon
        try:
            icon.run(setup=setup)
        finally:
            stop.set()
    finally:
        release_loop_lock(lock)
