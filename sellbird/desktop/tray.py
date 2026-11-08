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
import os
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import quote
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
    # What's waiting on you, from Today: conversations Sellbird handed to you
    # and emails waiting for your approval.
    needs_you: int = 0
    to_approve: int = 0

    @property
    def waiting(self) -> int:
        return self.needs_you + self.to_approve


@dataclass(frozen=True)
class Reply:
    """The newest message in a conversation, when the prospect wrote it."""
    key: tuple[str, int]          # (conversation id, messages in the thread)
    name: str
    company: str
    text: str
    intent: str = ""


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
        _, today = self._call("GET", "/api/today")
        stats = today.get("stats") if isinstance(today.get("stats"), dict) else {}
        return TrayStatus("running", "running" if loop.get("running") else "stopped",
                          sending_paused=loop.get("sending_paused") or "",
                          needs_you=int(stats.get("needs_human") or 0),
                          to_approve=int(stats.get("outbox_pending") or 0))

    def replies(self) -> list[Reply] | None:
        """Each conversation whose newest message is the prospect's, or None
        when the dashboard can't be read."""
        base = self._base()
        if base is None:
            return None
        token = _read_user_token(self.data_dir)
        try:
            with self._client(base) as http:
                r = http.get("/api/conversations", headers={"Authorization": f"Bearer {token}"})
            rows = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, OSError, ValueError):
            return None
        if not isinstance(rows, list):
            return None
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            thread = row.get("thread") or []
            if not thread or not isinstance(thread[-1], dict) or thread[-1].get("sender") != "prospect":
                continue
            name = " ".join(x for x in (row.get("first_name"), row.get("last_name")) if x) or "Someone"
            out.append(Reply((str(row.get("id")), len(thread)), name, row.get("company") or "",
                             str(thread[-1].get("content") or ""), row.get("intent") or ""))
        return out

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

    def dashboard_link(self, tab: str = "") -> str:
        """The dashboard's address with your token, or "" when it isn't running."""
        info = dashboard_instance.read(self.data_dir)
        if info is None:
            return ""
        from sellbird.mcp_server import base_url
        token = _read_user_token(self.data_dir)
        return base_url(info.host, info.port) + (f"#token={token}" if token else "#")

    def open_dashboard(self) -> bool:
        if self._base() is None:
            return False
        link = self.dashboard_link()
        if not link:
            return False
        self._opener(link)
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


def waiting_label(status: TrayStatus) -> str:
    """"2 replies need you · 8 emails to approve", or "" when nothing waits."""
    parts = []
    if status.needs_you:
        parts.append(f"{status.needs_you} {'reply needs' if status.needs_you == 1 else 'replies need'} you")
    if status.to_approve:
        parts.append(f"{status.to_approve} {'email' if status.to_approve == 1 else 'emails'} to approve")
    return " · ".join(parts)


def reply_message(reply: Reply) -> str:
    """The notification for one new reply."""
    who = f"{reply.name} at {reply.company}" if reply.company else reply.name
    text = " ".join(reply.text.split())
    if len(text) > 90:
        text = text[:87].rstrip() + "..."
    verb = "said yes to your offer" if reply.intent == "accepted_offer" else "replied"
    return f"{who} {verb}: \u201c{text}\u201d"


def new_reply_messages(seen: set, replies: list[Reply], limit: int = 3) -> list[tuple[str, str]]:
    """(message, conversation id) for replies not in ``seen`` (which this
    updates). More than ``limit`` at once become one summary, with no single
    conversation to open."""
    new = [r for r in replies if r.key not in seen]
    seen.update(r.key for r in replies)
    if len(new) > limit:
        return [(f"{len(new)} new replies. Open Sellbird to read them.", "")]
    return [(reply_message(r), r.key[0]) for r in new]


def menu_items(status: TrayStatus, autostart_on: bool) -> list[MenuRow]:
    up = status.dashboard == "running" and not status.error
    rows = [MenuRow(f"Sellbird: {state_label(status)}", None, False)]
    if up and status.waiting:
        rows.append(MenuRow(waiting_label(status), "open", True))
    rows.append(MenuRow("Open dashboard", "open", status.dashboard == "running"))
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


# The menu-bar icon in points (macOS draws it at 2x on Retina screens).


def draw_icon(state: str, dark: bool, width: int, height: int):
    """The Sellbird swallow filling a width x height pixel image, with a dot
    in the state's color tucked under the wing. Drawn at 4x and shrunk, so
    edges stay smooth at any size."""
    from PIL import Image, ImageDraw

    ss = 4
    w, h = width * ss, height * ss
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    r, ring = h * 0.14, h * 0.05
    # the bird fills the height; a wide image keeps the dot clear of it on the right
    unit = min((w - (r if w > h * 1.4 else 0)) / BIRD_BOX[0], h * 0.94 / BIRD_BOX[1])
    bird_w, bird_h = BIRD_BOX[0] * unit, BIRD_BOX[1] * unit
    left, top = 0 if w > h * 1.4 else (w - bird_w) / 2, (h - bird_h) / 2
    draw.polygon(bird_points(bird_w, bird_h, left, top),
                 fill="#ffffff" if dark else "#1d1d1f")
    cx, cy = w - r - ring, h - r - ring
    hole = Image.new("L", (w, h), 0)  # a clear ring so the dot reads over the wing
    ImageDraw.Draw(hole).ellipse((cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring),
                                 fill=255)
    image.paste((0, 0, 0, 0), mask=hole)
    ImageDraw.Draw(image).ellipse((cx - r, cy - r, cx + r, cy + r),
                                  fill=COLORS.get(state, COLORS["stopped"]))
    return image.resize((width, height), Image.LANCZOS)


def make_icon(state: str, dark: bool | None = None):
    """64x64, for the Windows tray (pystray scales it to the tray's size)."""
    return draw_icon(state, _dark_bar() if dark is None else dark, 64, 64)


MENUBAR_HEIGHT_PT = 18
MENUBAR_BIRD_PT = 13.0      # about the height of the other menu-bar glyphs
BRAND = "#244B62"           # Sellbird harbor blue
_BADGE_FONT = "/System/Library/Fonts/SFNSRounded.ttf"


def _badge_font(size: int):
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(_BADGE_FONT, size)
        try:
            f.set_variation_by_name("Bold")
        except Exception:
            pass
        return f
    except OSError:
        return ImageFont.load_default()


def draw_menubar(state: str, dark: bool, count: int = 0, scale: int = 2):
    """The macOS menu-bar icon: the bird at the height of the other glyphs,
    and beside it a dot in the state's color, or, when something waits on
    you, a small bubble in that color with the count. Returns the image (at
    ``scale``x) and its size in points."""
    from PIL import Image, ImageDraw

    ss = 4 * scale
    H = MENUBAR_HEIGHT_PT * ss
    bird_h = MENUBAR_BIRD_PT * ss
    bird_w = BIRD_BOX[0] * bird_h / BIRD_BOX[1]
    text = "" if not count else ("99+" if count > 99 else str(count))
    font = _badge_font(int(6.6 * ss))
    if text:
        box = font.getbbox(text)
        bh = 9.2 * ss
        bw = max(bh, box[2] - box[0] + 5 * ss)
    else:
        bh = bw = 5.6 * ss
    ring = 1.4 * ss
    W = bird_w + bw * (0.9 if text else 0.55) + ring
    image = Image.new("RGBA", (int(W), int(H)), (0, 0, 0, 0))
    ImageDraw.Draw(image).polygon(bird_points(bird_w, bird_h, 0, (H - bird_h) / 2),
                                  fill="#ffffff" if dark else "#1d1d1f")
    x1, y1 = W - ring, H - ring * 0.6
    x0, y0 = x1 - bw, y1 - bh
    hole = Image.new("L", image.size, 0)  # a clear ring so the bubble reads over the bird
    ImageDraw.Draw(hole).rounded_rectangle((x0 - ring, y0 - ring, x1 + ring, y1 + ring),
                                           radius=(bh + 2 * ring) / 2, fill=255)
    image.paste((0, 0, 0, 0), mask=hole)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=bh / 2,
                           fill=COLORS.get(state, COLORS["stopped"]))
    if text:
        box = font.getbbox(text)
        draw.text((x0 + (bw - (box[2] - box[0])) / 2 - box[0],
                   y0 + (bh - (box[3] - box[1])) / 2 - box[1]), text, font=font, fill="#ffffff")
    out = image.resize((max(1, int(W / 4)), int(H / 4)), Image.LANCZOS)
    return out, (W / ss, float(MENUBAR_HEIGHT_PT))


def menubar_png(state: str, dark: bool, count: int = 0, scale: int = 2) -> tuple[bytes, tuple]:
    """The menu-bar icon as PNG bytes at ``scale``x, and its size in points."""
    import io

    image, size = draw_menubar(state, dark, count, scale)
    out = io.BytesIO()
    image.save(out, "png")
    return out.getvalue(), size


def app_icon(size: int = 256):
    """Sellbird's app icon: the white bird on a harbor-blue rounded square.
    Notifications show it."""
    from PIL import Image, ImageDraw

    S = size * 4
    image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = S * 0.08
    draw.rounded_rectangle((pad, pad, S - pad, S - pad), radius=S * 0.2, fill=BRAND)
    bw = (S - 2 * pad) * 0.74
    bh = bw * BIRD_BOX[1] / BIRD_BOX[0]
    draw.polygon(bird_points(bw, bh, (S - bw) / 2, (S - bh) / 2 + S * 0.01), fill="#ffffff")
    return image.resize((size, size), Image.LANCZOS)


def _sharp_on_retina(icon) -> None:
    """pystray's macOS backend shrinks the icon to the bar's thickness in
    pixels (22), so a Retina screen shows it blurry and square. Hand AppKit a
    2x PNG with its size set in points instead."""
    if sys.platform != "darwin":
        return

    def assert_image():
        import AppKit
        import Foundation

        look = getattr(icon, "_sellbird_look", ("stopped", _dark_bar(), 0))
        if getattr(icon, "_sellbird_drawn", None) == look and icon._icon_image:
            return
        png, size = menubar_png(*look)
        image = AppKit.NSImage.alloc().initWithData_(Foundation.NSData(png))
        image.setSize_(size)
        icon._icon_image = image
        icon._sellbird_drawn = look
        icon._status_item.button().setImage_(image)

    icon._assert_image = assert_image


# ── The tray app ──

NOTIFIER_SOURCE = Path(__file__).with_name("notifier") / "main.swift"
# Built from NOTIFIER_SOURCE for Apple silicon and Intel and shipped with
# Sellbird, so a Mac without Xcode still gets branded notifications. The
# .source-sha256 file names the source it was built from: when the source
# changes and the binary isn't rebuilt, it's ignored and swiftc builds one.
NOTIFIER_PREBUILT = NOTIFIER_SOURCE.with_name("sellbird-notify")


def prebuilt_notifier() -> Path | None:
    """The shipped notifier binary, when it matches the source next to it."""
    import hashlib
    stamp = NOTIFIER_PREBUILT.with_name("sellbird-notify.source-sha256")
    try:
        built_from = stamp.read_text(encoding="utf-8").strip()
        source = hashlib.sha256(NOTIFIER_SOURCE.read_bytes()).hexdigest()
    except OSError:
        return None
    return NOTIFIER_PREBUILT if NOTIFIER_PREBUILT.is_file() and built_from == source else None
NOTIFIER_ID = "ai.sellbird.notifier"
_LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
               "LaunchServices.framework/Support/lsregister")
_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>{id}</string>
<key>CFBundleName</key><string>Sellbird</string>
<key>CFBundleDisplayName</key><string>Sellbird</string>
<key>CFBundleExecutable</key><string>sellbird-notify</string>
<key>CFBundleIconFile</key><string>Sellbird</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>{version}</string>
<key>LSUIElement</key><true/>
</dict></plist>
"""


def notifier_version() -> str:
    """Changes whenever the helper's source or icon would."""
    import hashlib
    h = hashlib.sha256(NOTIFIER_SOURCE.read_bytes())
    h.update(BRAND.encode() + BIRD_PATH.encode())
    return h.hexdigest()[:12]


def build_notifier(app: Path, run=subprocess.run) -> bool:
    """Build ``app``: a tiny macOS app named Sellbird, with the bird as its
    icon, that posts notifications (sellbird/desktop/notifier/main.swift).

    macOS shows a notification with the icon of the app that sent it, and
    only lets a real, signed app send one: from Python they came out as
    "python3" or Script Editor. Needs the Swift compiler (Xcode or its
    command-line tools) only when the shipped binary (NOTIFIER_PREBUILT) is
    missing or out of date; without either it keeps the old style."""
    import shutil
    import tempfile

    version = notifier_version()
    stamp = app / "Contents" / "sellbird-version"
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == version:
        return True
    prebuilt = prebuilt_notifier()
    swiftc = None if prebuilt else shutil.which("swiftc")
    if prebuilt is None and swiftc is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "Sellbird.app"
        (work / "Contents" / "MacOS").mkdir(parents=True)
        (work / "Contents" / "Resources").mkdir()
        iconset = Path(tmp) / "Sellbird.iconset"
        iconset.mkdir()
        for px in (16, 32, 64, 128, 256, 512, 1024):
            image = app_icon(px)
            if px <= 512:
                image.save(iconset / f"icon_{px}x{px}.png")
            if px >= 32:
                image.save(iconset / f"icon_{px // 2}x{px // 2}@2x.png")
        binary = work / "Contents" / "MacOS" / "sellbird-notify"
        steps = [["iconutil", "-c", "icns", str(iconset), "-o",
                  str(work / "Contents" / "Resources" / "Sellbird.icns")]]
        if prebuilt:
            shutil.copy2(prebuilt, binary)
            binary.chmod(0o755)
        else:
            steps.insert(0, [swiftc, "-O", "-o", str(binary), str(NOTIFIER_SOURCE)])
        (work / "Contents" / "Info.plist").write_text(
            _INFO_PLIST.format(id=NOTIFIER_ID, version=version), encoding="utf-8")
        for step in steps:
            if run(step, capture_output=True).returncode != 0:
                logger.debug("Notifier build step failed: %s", step[0])
                return False
        (work / "Contents" / "sellbird-version").write_text(version, encoding="utf-8")
        if app.exists():
            shutil.rmtree(app)
        app.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, app)
    # Ad-hoc signed and registered, so macOS knows Sellbird by name and icon.
    run(["codesign", "--force", "--deep", "-s", "-", str(app)], capture_output=True)
    run([_LSREGISTER, "-f", str(app)], capture_output=True)
    return True


class MacNotifier:
    """Posts notifications through the Sellbird helper app (build_notifier),
    so they show Sellbird's name and icon, and a click opens the dashboard."""

    def __init__(self, app: Path, link_file: Path):
        self.app, self.link_file = app, link_file

    @classmethod
    def create(cls, data_dir: Path, app: Path | None = None):
        """A notifier, or False when this Mac can't build the helper."""
        # One helper per user, where macOS lets an app send notifications
        # (from a temporary or project folder it refuses them outright).
        app = app or (Path.home() / "Library" / "Application Support" / "sellbird" / "Sellbird.app")
        try:
            if not build_notifier(app):
                return False
        except Exception as e:
            logger.debug("Notifier not built: %s", type(e).__name__)
            return False
        return cls(app, Path(data_dir) / "notify-link")

    def notify(self, message: str, link: str = "") -> bool:
        """Post ``message``. ``link`` (the dashboard, with its token) is kept
        in a file only you can read, not on a command line others could see."""
        try:
            if link:
                self.link_file.write_text(link, encoding="utf-8")
                os.chmod(self.link_file, 0o600)
            subprocess.Popen(["open", "-n", "-g", str(self.app), "--args", message,
                              str(self.link_file) if link else ""],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as e:
            logger.debug("Notification not shown: %s", type(e).__name__)
            return False


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
        self._drawn: tuple | None = None
        self._lock = threading.Lock()
        # Replies already seen. None until the first look, which notifies
        # nothing: only replies that arrive while the tray runs pop up.
        self._seen: set | None = None
        self._mac = None   # MacNotifier, made on first use (False when unavailable)

    def refresh(self, *, autostart: bool = False) -> None:
        status = self.controller.status()
        autostart_on = self._autostart() if autostart else None
        with self._lock:
            self.status = status
            if autostart_on is not None:
                self.autostart_on = autostart_on
        self._ui(lambda: self._apply(status))
        if status.dashboard == "running" and not status.error:
            self._check_replies()

    def _check_replies(self) -> None:
        replies = self.controller.replies()
        if replies is None:
            return
        if self._seen is None:
            self._seen = {r.key for r in replies}
            return
        for message, conversation in new_reply_messages(self._seen, replies):
            self._notify(message, conversation=conversation)

    def _apply(self, status: TrayStatus) -> None:
        if self.icon is not None:
            count = status.waiting if status.dashboard == "running" and not status.error else 0
            look = (icon_state(status), _dark_bar(), count)
            if look != self._drawn:  # redraw only when the state, bar color or count changes
                self.icon._sellbird_look = look
                self.icon.icon = make_icon(look[0], look[1])
                self._drawn = look
            self.icon.update_menu()

    def _notify(self, message: str, conversation: str | None = None) -> None:
        """Show ``message``. A reply's notification (``conversation`` given,
        "" for a summary of several) opens Conversations when clicked, at
        that conversation; anything else opens the dashboard."""
        def show():
            if self._mac is None and sys.platform == "darwin":
                self._mac = MacNotifier.create(self.controller.data_dir)
            if self._mac:
                link = self.controller.dashboard_link()
                if link and conversation is not None:
                    link += "&tab=conversations" + (f"&focus={quote(conversation, safe='')}"
                                                    if conversation else "")
                if self._mac.notify(message, link):
                    return
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
        _sharp_on_retina(icon)
        app.icon = icon
        try:
            icon.run(setup=setup)
        finally:
            stop.set()
    finally:
        release_loop_lock(lock)
