"""Harvey CLI — simple commands to install, setup, run, and manage Harvey."""

import argparse
import asyncio
import subprocess
import sys


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    print("\n  Installing Harvey dependencies...\n")

    # Install Python packages
    print("  [1/2] Installing Python packages...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=args._project_root,
    )
    if result.returncode != 0:
        print("\n  Failed to install Python packages.")
        sys.exit(1)
    print("  ✓ Python packages installed.\n")

    # Install Playwright browsers
    print("  [2/2] Installing Playwright browsers...")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
    )
    if result.returncode != 0:
        print("\n  Playwright browser install failed (optional — needed for LinkedIn).")
    else:
        print("  ✓ Playwright browsers installed.\n")

    print("  Harvey is installed. Run 'harvey setup' next.\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from harvey.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start Harvey's heartbeat loop."""
    from harvey.main import main

    main()


def cmd_train(args):
    """Train Harvey on a website."""
    from harvey.trainer import Trainer

    trainer = Trainer()
    asyncio.run(trainer.train_from_url(args.url, max_pages=args.max_pages))


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from harvey.dashboard import start_dashboard

    start_dashboard(host=args.host, port=args.port)


def cmd_status(args):
    """Show current pipeline status."""
    from harvey.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Harvey Pipeline Status")
        print("  " + "=" * 40)
        print(f"  Prospects:            {summary['prospects']}")
        print(f"  Draft campaigns:      {summary['draft_campaigns']}")
        print(f"  Active campaigns:     {summary['active_campaigns']}")
        print(f"  Open conversations:   {summary['open_conversations']}")
        print(f"  Claude calls today:   {summary['usage_today']}")
        print()

    asyncio.run(_status())


def main():
    from pathlib import Path

    project_root = str(Path(__file__).parent.parent)

    parser = argparse.ArgumentParser(
        prog="harvey",
        description="Harvey — Autonomous AI Sales Agent. Always Be Closing.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # harvey install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # harvey setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # harvey run
    sub = subparsers.add_parser("run", help="Start Harvey's heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # harvey train <url>
    sub = subparsers.add_parser("train", help="Train Harvey on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    sub.set_defaults(func=cmd_train)

    # harvey dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.set_defaults(func=cmd_dashboard)

    # harvey status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args._project_root = project_root

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    harvey install   — Install dependencies")
        print("    harvey setup     — Configure Harvey (first time)")
        print("    harvey run       — Start closing deals")
        print("    harvey dashboard — Open the web dashboard")
        print()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
