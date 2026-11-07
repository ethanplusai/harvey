"""Desktop commands: `sellbird autostart install|uninstall|status`.

The login item starts the tray (`sellbird tray`) when the user logs in. The
logic lives in sellbird.desktop.login_item; this only parses and prints.
Exceptions print their message and exit 1, with no traceback.
"""

import subprocess
import sys


def register_desktop_commands(subparsers) -> None:
    sub = subparsers.add_parser("autostart", help="Start the Sellbird tray when you log in")
    sub.add_argument("autostart_action", choices=["install", "uninstall", "status"])
    sub.add_argument("--port", type=int, default=None,
                     help="Dashboard port the tray uses (default: the tray's own default)")
    sub.set_defaults(func=cmd_autostart)


def cmd_autostart(args, *, platform=None, runner=subprocess.run, env=None, home=None):
    from sellbird.desktop import login_item as li

    common = dict(platform=platform, runner=runner, env=env, home=home)
    try:
        if args.autostart_action == "install":
            print(f"  {li.install(port=args.port, **common).message}")
        elif args.autostart_action == "uninstall":
            print(f"  {li.uninstall(**common).message}")
        else:
            state = li.status(**common)
            if not state["installed"]:
                print("  Start at login is off.")
            elif state["ours"]:
                print(f"  Start at login is on ({state['path']}).")
            else:
                print(f"  Start at login is off. {state['path']} exists but wasn't "
                      "created by Sellbird.")
    except (li.UnsupportedPlatform, li.ForeignLoginItem, li.LoginItemFailed) as e:
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)


# ── `sellbird tray` ──

def register(subparsers) -> None:
    sub = subparsers.add_parser("tray", help="Menu-bar icon: start, stop and open Sellbird")
    sub.add_argument("--status", action="store_true",
                     help="Print whether it's running and the next setup step, then exit")
    sub.add_argument("--port", type=int, default=None,
                     help="Dashboard port (default: the last one given, else 5555)")
    sub.set_defaults(func=cmd_tray)


def cmd_tray(args, *, data_dir=None, transport=None, autostart=None):
    """Show the tray icon, or with --status print two lines. Neither prints
    a URL or a token. No tray on this platform: one stderr line, exit 0 (the
    login item relaunches only after a failure)."""
    import logging
    from pathlib import Path

    from sellbird.desktop import tray
    from sellbird.paths import PROJECT_ROOT

    data_dir = Path(data_dir) if data_dir is not None else PROJECT_ROOT / "data"
    if args.port is not None and not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.", file=sys.stderr)
        sys.exit(2)
    port = tray.resolve_port(data_dir, args.port)
    if args.status:
        status = tray.TrayController(data_dir, port=port, transport=transport).status()
        print(f"  Sellbird: {tray.state_label(status)}")
        if status.error:
            print(f"  {status.error}")
        elif status.dashboard != "running":
            print("  Start it with `sellbird tray` or `sellbird dashboard`.")
        elif status.sending_paused:
            print(f"  Sending is paused: {status.sending_paused}")
        return
    if args.port is not None:
        tray.remember_port(data_dir, args.port)
    from sellbird.logsafety import RedactSecrets
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    handler.addFilter(RedactSecrets())
    tray_logger = logging.getLogger("sellbird.tray")
    tray_logger.addHandler(handler)
    tray_logger.setLevel(logging.INFO)
    try:
        tray.run_tray(data_dir, port=port,
                      controller=tray.TrayController(data_dir, port=port, transport=transport),
                      autostart=autostart)
    except tray.TrayUnavailable as e:
        print(f"  {e}", file=sys.stderr)
    finally:
        tray_logger.removeHandler(handler)
