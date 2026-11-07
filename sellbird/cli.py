"""Sellbird CLI — simple commands to install, setup, run, and manage Sellbird."""

import argparse
import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path


MIN_PYTHON = (3, 11)


def _check_python_version():
    """Refuse to install on a Python that cannot run Sellbird.

    `python3 -m venv .venv` on macOS builds the venv from /usr/bin/python3,
    which is 3.9 — old enough that Sellbird's `X | Y` type syntax fails at
    import. The install itself appears to succeed and the failure surfaces
    later as an unrelated-looking ImportError, so check up front and name the
    fix.
    """
    if sys.version_info >= MIN_PYTHON:
        return

    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    print(f"\n  Sellbird needs Python {want} or newer. This is Python {have}.")
    print(f"  ({sys.executable})\n")
    print("  On macOS, `python3` is usually the system 3.9, so a venv built")
    print("  with it is 3.9 too. Build the venv from a newer Python instead:\n")
    print("    brew install python@3.13")
    print("    rm -rf .venv")
    print("    $(brew --prefix)/bin/python3.13 -m venv .venv")
    print("    source .venv/bin/activate && pip install -e .\n")
    sys.exit(1)


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    _check_python_version()
    print("\n  Installing Sellbird dependencies...\n")

    # An installed app (pipx, pip) already has its packages; only a checkout
    # installs them, from the checkout (not the home folder).
    from sellbird.paths import PACKAGE_ROOT, is_checkout
    checkout = is_checkout(PACKAGE_ROOT)
    if checkout:
        print("  [1/2] Installing Python packages...")
        requirements = PACKAGE_ROOT / "requirements.txt"
        # Fall back to an editable install from pyproject.toml
        pip_args = ["-r", "requirements.txt"] if requirements.exists() else ["-e", "."]
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", *pip_args],
            cwd=str(PACKAGE_ROOT),
        )
        if result.returncode != 0:
            print("\n  Failed to install Python packages.")
            print("  Tip: make sure you're inside a virtualenv "
                  "(python3 -m venv .venv && source .venv/bin/activate).")
            sys.exit(1)
        print("  ✓ Python packages installed.\n")
    else:
        print("  [1/2] Python packages: already part of this install.")

    # Install Playwright browsers
    print("  [2/2] Installing Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
        )
        playwright_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        playwright_ok = False
    if not playwright_ok:
        print("\n  Playwright browser install failed (optional — needed for LinkedIn).")
    else:
        print("  ✓ Playwright browsers installed.\n")

    if checkout:
        _ensure_importable(str(PACKAGE_ROOT))
    print("  Sellbird is installed. Run 'sellbird setup' next.\n")


def _ensure_importable(project_root: str):
    """Make sure `import sellbird` works outside the repo directory.

    Python 3.13 silently skips .pth files carrying the macOS 'hidden'
    file flag, and some Macs propagate that flag to everything inside
    dot-directories like .venv — which breaks editable installs. When
    that happens, fall back to symlinking the package into site-packages
    (imports don't check the hidden flag; only .pth parsing does).
    """
    check = subprocess.run(
        [sys.executable, "-c", "import sellbird"],
        cwd="/", capture_output=True,
    )
    if check.returncode == 0:
        return

    try:
        import site
        site_packages = Path(site.getsitepackages()[0])
        link = site_packages / "sellbird"
        target = Path(project_root) / "sellbird"
        if not link.exists() and target.is_dir():
            link.symlink_to(target)
            recheck = subprocess.run(
                [sys.executable, "-c", "import sellbird"],
                cwd="/", capture_output=True,
            )
            if recheck.returncode == 0:
                print("  ✓ Fixed package visibility (editable .pth was being "
                      "ignored; linked the package directly).\n")
                return
    except OSError as e:
        print(f"  Could not apply import fix: {e}")

    print("\n  Warning: 'import sellbird' fails outside the project directory.")
    print("  Run sellbird commands from the project root, or reinstall with:")
    print("    pip install -e . --config-settings editable_mode=compat\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from sellbird.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start Sellbird's heartbeat loop."""
    from sellbird.main import main

    main()


def cmd_train(args):
    """Train Sellbird on a website."""
    from sellbird.trainer import Trainer

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        print(f"  No scheme given — using {url}")

    if args.max_pages < 1:
        print("  max_pages must be at least 1.")
        sys.exit(2)

    from pydantic import ValidationError
    from sellbird.config import BrainConfig, ConfigError, load_brain_config

    try:
        brain_config = load_brain_config()
        overrides = {
            k: v for k, v in (
                ("provider", args.provider),
                ("model", args.model),
                ("reasoning_effort", args.reasoning_effort),
            ) if v is not None
        }
        if overrides:
            if "provider" in overrides and overrides["provider"] != brain_config.provider:
                # A different provider never inherits the other one's model.
                brain_config = BrainConfig(**overrides)
            else:
                brain_config = BrainConfig(**{**brain_config.model_dump(), **overrides})
    except (ConfigError, ValidationError) as e:
        print(f"  {e}")
        sys.exit(2)

    trainer = Trainer(brain_config=brain_config)
    asyncio.run(trainer.train(url, max_pages=args.max_pages))


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from sellbird.dashboard import DashboardSetupError, dashboard_url, start_dashboard
    from sellbird.dashboard_auth import TokenError

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    try:
        if getattr(args, "url", False):
            # The unlock link for an already-running dashboard.
            print(dashboard_url(host=args.host, port=args.port))
            return
        start_dashboard(host=args.host, port=args.port)
    except (DashboardSetupError, TokenError) as e:
        print(f"\n  Dashboard not started: {e}\n")
        sys.exit(2)


def cmd_status(args):
    """Show current pipeline status."""
    from sellbird.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Sellbird Pipeline Status")
        print("  " + "=" * 40)
        print(f"  Prospects:            {summary['prospects']}")
        print(f"  Ready to write for:   {summary.get('writable_prospects', 0)} "
              "(new, with a verified or catch-all email)")
        write_failed = (summary.get("prospects") or {}).get("write_failed", 0)
        if write_failed:
            print(f"  Couldn't write for:   {write_failed} (the model's drafts kept coming back "
                  "unusable; `sellbird retry-writes` tries again)")
        print(f"  Draft campaigns:      {summary['draft_campaigns']}")
        print(f"  Active campaigns:     {summary['active_campaigns']}")
        print(f"  Open conversations:   {summary['open_conversations']}")
        by_provider = summary.get("usage_today_by_provider", {})
        print(f"  Claude calls today:   {by_provider.get('claude', summary['usage_today'])}")
        print(f"  Codex calls today:    {by_provider.get('codex', 0)}")
        from sellbird.config import compliance_notice, load_config, sending_notice
        loaded = None
        try:
            loaded = load_config()
            notice = sending_notice(loaded)
            address_notice = compliance_notice(loaded)
        except Exception:
            # Status must still print when sellbird.yaml is broken; that's
            # when people run it. The --check path reports config errors.
            notice = address_notice = ""
        from sellbird.main import unusable_backlog_notice
        backlog = unusable_backlog_notice(summary)
        if backlog:
            print("  Prospecting:          PAUSED")
            print(textwrap.fill(backlog, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        if notice:
            print("  Sending:              HELD")
            print(textwrap.fill(notice, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        from sellbird.agents.writer import EMAIL_RULES_PROBLEM_KEY
        rules_problem = await state.get_setting(EMAIL_RULES_PROBLEM_KEY)
        if rules_problem:
            print(f"  Writing:              STOPPED (writer.email_rules: {rules_problem})")
        hold = await state.current_sends_held()
        if hold:
            from sellbird.sending_mode import held_message
            waiting = len(await state.get_outbox(status="approved", limit=500))
            title, detail = held_message(hold, waiting=waiting)
            print(f"  Sending:              NOT SENDING ({title.split(': ', 1)[-1]})")
            print(textwrap.fill(detail, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        from sellbird.integrations.precleared import current_hold, hold_message
        precleared_hold = await current_hold(state, loaded)
        if precleared_hold:
            message = hold_message(precleared_hold)
            print(f"  Precleared:           HOLDING COLD EMAIL since {precleared_hold.get('since', '?')}")
            print(textwrap.fill(f"{message['title']}. {message['detail']}", width=72,
                                initial_indent="                        ",
                                subsequent_indent="                        "))
        if address_notice:
            print("  Postal address:       MISSING")
            print(textwrap.fill(address_notice, width=72, initial_indent="                        ",
                                subsequent_indent="                        "))
        from sellbird.brain import BRAIN_STATUS_KEY, Brain, active_brain_status
        from sellbird.config import ConfigError, load_brain_config
        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            brain_config, config_error = None, e
        # Only the configured provider's health: an old Codex pause says
        # nothing once sellbird.yaml has switched to Claude.
        brain_status = active_brain_status(
            await state.get_setting(BRAIN_STATUS_KEY),
            brain_config.provider if brain_config else "")
        healthy = True
        if getattr(args, "check", False):
            if brain_config is None:
                print(f"  Brain:                config error: {config_error}")
                healthy = False
            else:
                brain = Brain(state, brain_config)
                problem = await brain.preflight()
                label = brain.provider_label + (f" ({brain.model})" if brain.model else "")
                if problem:
                    healthy = False
                    print(f"  Brain:                {label} NOT READY")
                    print(f"                        {problem}")
                elif brain.provider == "codex":
                    print(f"  Brain:                {label} ready (CLI, isolation, login, "
                          "model and account checked; no prompt sent)")
                else:
                    print(f"  Brain:                {label} CLI isolation flags present "
                          "(login not checked: Claude is checked by its first call)")
        elif brain_status.get("status") == "paused":
            label = {"claude": "Claude", "codex": "Codex"}.get(brain_status.get("provider"), "Brain")
            print(f"  Brain:                {label} PAUSED since {brain_status.get('since', '?')}")
            print(f"                        {brain_status.get('reason', '')}")
        print()
        return healthy

    if not asyncio.run(_status()):
        # `sellbird status --check` failing is something scripts can act on.
        raise SystemExit(1)


def cmd_usage(args):
    """Show brain usage: active provider's quota, totals, breakdowns."""
    from sellbird.state import StateManager
    from sellbird.config import ConfigError, load_brain_config

    async def _usage():
        state = StateManager()
        await state.init_db()

        try:
            brain_config = load_brain_config()
        except ConfigError as e:
            print(f"  {e}")
            brain_config = None

        # Live quota for the ACTIVE provider only (best-effort). Each
        # subscription's gauge is read from its own source and labelled as
        # such; one is never shown as the other.
        windows = None
        provider = brain_config.provider if brain_config else None
        try:
            if provider == "codex":
                from sellbird.codex import CodexClient, CodexQuotaClient
                windows = await CodexQuotaClient(
                    CodexClient(model=brain_config.model)).get_utilization()
            elif provider == "claude":
                from sellbird.integrations.quota import QuotaClient
                windows = await QuotaClient().get_utilization()
        except Exception:
            pass

        label = {"claude": "Claude", "codex": "Codex"}.get(provider, "Brain")
        print(f"\n  {label} Usage")
        print("  " + "=" * 52)
        if windows:
            labels = {"five_hour": "5-hour window", "seven_day": "Weekly",
                      "limit_reached": "Usage limit"}
            for key, w in windows.items():
                resets = f"  (resets {w['resets_at']})" if w.get("resets_at") else ""
                print(f"  {labels.get(key, key):<16} {w['utilization']:5.1f}% used{resets}")
        elif provider == "codex":
            print("  Quota gauge unavailable (check 'codex login', the Codex CLI "
                  "version, and network).")
        elif provider == "claude":
            print("  Quota gauge unavailable (run 'claude login' or check network).")

        totals = await state.usage_totals()
        print()
        print(f"  {'Period':<10} {'Calls':>7} {'Input':>12} {'Output':>10} {'Cache read':>12}")
        for label, key in (("Today", "today"), ("7 days", "week"), ("30 days", "month")):
            t = totals.get(key) or {}
            print(
                f"  {label:<10} {t.get('calls', 0):>7} "
                f"{t.get('input_tokens', 0):>12,} {t.get('output_tokens', 0):>10,} "
                f"{t.get('cache_read_tokens', 0):>12,}"
            )

        by_provider = await state.usage_by_provider(days=args.days)
        if len(by_provider) > 1 or (by_provider and by_provider[0]["provider"] != "claude"):
            print(f"\n  By provider (last {args.days} days):")
            for row in by_provider:
                print(
                    f"    {row['provider']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_agent = await state.usage_by_agent(days=args.days)
        if by_agent:
            print(f"\n  By agent (last {args.days} days):")
            for row in by_agent:
                print(
                    f"    {row['agent']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_task = await state.usage_by_task(days=args.days)
        if by_task:
            print(f"\n  By task (last {args.days} days):")
            for row in by_task[:10]:
                print(
                    f"    {row['task']:<22} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )
        print(
            "\n  Subscription plans aren't billed per token — these are usage"
            "\n  counts, not costs.\n"
        )

    asyncio.run(_usage())


def cmd_companies(args):
    """Write the company register to a CSV and summarise it."""
    from collections import Counter
    from datetime import date
    from sellbird.config import load_config
    from sellbird.paths import PROJECT_ROOT
    from sellbird.register import build_register, sqlite_query, to_csv
    from sellbird.state import DB_PATH

    if not DB_PATH.exists():
        print("\n  No database yet: run 'sellbird discover' or 'sellbird run' first.\n")
        return
    rows = asyncio.run(build_register(sqlite_query(DB_PATH), load_config()))
    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "data" / "exports" / f"company-register-{date.today():%Y-%m-%d}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so spreadsheet apps open non-ASCII company names correctly.
    out.write_text(to_csv(rows), encoding="utf-8-sig")
    print(f"\n  Company register: {len(rows)} companies → {out}")
    for status, n in Counter(r["status"] for r in rows).most_common():
        print(f"    {status:<18} {n}")
    print()


def cmd_export(args):
    """Export the prospect list as a sequencer-ready CSV."""
    from sellbird.state import StateManager
    from sellbird.export import export_prospects_csv

    async def _export():
        state = StateManager()
        await state.init_db()

        email_statuses = None
        if args.email_status:
            email_statuses = [s.strip() for s in args.email_status.split(",") if s.strip()]
        statuses = None
        if args.status:
            statuses = [s.strip() for s in args.status.split(",") if s.strip()]

        count, _ = await export_prospects_csv(
            state,
            out_path=args.out,
            email_statuses=email_statuses,
            min_score=args.min_score,
            statuses=statuses,
            include_all=args.all,
        )
        scope = "all prospects" if args.all else (
            f"email status {email_statuses or ['verified', 'risky']}"
            + (f", score >= {args.min_score}" if args.min_score else "")
        )
        print(f"\n  Exported {count} prospect(s) to {args.out}  ({scope})")
        if count == 0 and not args.all:
            print("  Tip: no deliverable emails yet? Add a REOON_API_KEY to .env so")
            print("  Sellbird can verify addresses, or use --all for the raw list.\n")
        else:
            print("  The CSV imports directly into Instantly, Smartlead, or any sequencer.\n")

    asyncio.run(_export())


def cmd_gmail(args):
    """Gmail provider utilities (auth / test)."""
    from sellbird.config import load_env

    env = load_env()
    if args.gmail_action == "auth":
        from sellbird.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret)
        sys.exit(0 if ok else 1)

    if args.gmail_action == "test":
        from sellbird.config import load_config
        from sellbird.integrations.gmail import GmailProvider

        async def _test():
            provider = GmailProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_microsoft(args):
    """Microsoft 365 provider utilities (auth / test)."""
    from sellbird.config import load_env

    env = load_env()
    if args.microsoft_action == "auth":
        from sellbird.integrations.microsoft import run_auth_flow
        ok = run_auth_flow(env.m365_client_id, env.m365_tenant_id)
        sys.exit(0 if ok else 1)

    if args.microsoft_action == "test":
        from sellbird.config import load_config
        from sellbird.integrations.microsoft import MicrosoftProvider

        async def _test():
            provider = MicrosoftProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_outbox(args):
    """Review and approve queued outgoing emails."""
    from sellbird.state import StateManager

    async def _outbox():
        state = StateManager()
        await state.init_db()

        if args.approve_all:
            n = await state.approve_outbox()
            print(f"\n  Approved {n} email(s). They'll send on schedule.")
            held = await state.count_held_for_review()
            if held:
                print(f"  {held} email(s) Sellbird held for review were left out: read each one,")
                print("  then approve it with --approve <id>.")
            print()
            return
        if args.approve:
            n = await state.approve_outbox(args.approve)
            print(f"\n  {'Approved.' if n else 'No pending item with that id.'}\n")
            return
        if args.reject:
            n = await state.reject_outbox_item(args.reject)
            print(f"\n  {'Rejected.' if n else 'Nothing waiting to send with that id (already sending or sent?).'}\n")
            return

        paused = await state.get_setting("sending_paused")
        if paused:
            print(f"\n  ⚠ SENDING PAUSED: {paused}")
            print("  Resume with: sellbird sending resume")

        pending = await state.get_outbox(status="pending_review", limit=50)
        approved = await state.get_outbox(status="approved", limit=10)
        print(f"\n  Outbox — {len(pending)} awaiting approval, "
              f"{len(approved)}+ approved/scheduled")
        print("  " + "=" * 60)
        for item in pending:
            print(f"\n  [{item['id']}] step {item['step']} ({item['kind']}) "
                  f"→ {item['to_email']}  (send {item['send_at'][:16]})")
            print(f"  Subject: {item['subject']}")
            body_preview = (item["body"][:200] + "...") if len(item["body"]) > 200 else item["body"]
            for line in body_preview.splitlines():
                print(f"    {line}")
        if pending:
            print("\n  Approve: sellbird outbox --approve <id>   |   all: sellbird outbox --approve-all")
            print("  Reject:  sellbird outbox --reject <id>\n")
        else:
            print("  Nothing awaiting review.\n")

    asyncio.run(_outbox())


def cmd_profile(args):
    """Read what companies' own websites say about them. Free, no Claude calls."""
    from sellbird.state import StateManager
    from sellbird.pipeline import run_profile_stage

    async def _profile():
        state = StateManager()
        await state.init_db()

        pending = await state.count_companies_needing_profile(args.stale_days)
        if not pending:
            print("\n  Nothing to profile. Every company with a website has "
                  "been read recently.\n")
            return

        print(f"\n  {pending} companies need profiling. Reading up to "
              f"{args.limit}...\n")
        try:
            from sellbird.config import load_config
            config = load_config()
        except Exception:
            config = None   # still profiles; no role is singled out as hiring intent
        companies, observations, _ = await run_profile_stage(
            state, limit=args.limit, stale_days=args.stale_days, config=config)
        print(f"  Read {companies} sites → {observations} observations (free)")
        print("\n  See what turned up: sellbird dashboard → Signals\n")

    asyncio.run(_profile())


def cmd_discover(args):
    """Find businesses. The only stage that spends money, so it estimates first."""
    from sellbird.state import StateManager
    from sellbird.config import load_config, load_env
    from sellbird.collectors.discover import (
        PROVIDERS, build_queries, preset_problem, provider_menu, run_discovery,
    )

    async def _discover():
        if args.providers:
            print("\n  Discovery providers")
            print("  " + "=" * 68)
            for m in provider_menu(load_env().model_dump()):
                mark = "ready" if m["configured"] else "needs setup"
                print(f"\n  {m['label']}  [{m['key']}]  ({mark})")
                print(f"    {m['blurb']}")
                print(f"    Cost: {m['cost_note']}")
                print(f"    Free: {m['free_tier']}")
                if m["env_keys"]:
                    print(f"    Needs: {', '.join(m['env_keys'])} in .env  —  {m['signup_url']}")
                if m["caveat"]:
                    print(f"    Note: {m['caveat']}")
            print("\n  Run one with: sellbird discover --provider <key> --estimate\n")
            return

        if args.provider not in PROVIDERS:
            print(f"\n  Unknown provider {args.provider!r}. "
                  f"See: sellbird discover --providers\n")
            return

        # Fail before collection; estimates need no credentials.
        # An unconfigured provider used to end in one quiet "stopped
        # early" line and exit code 0: a silent no-op that reads as
        # success.
        provider = PROVIDERS[args.provider]
        missing = provider.missing_keys(load_env().model_dump())
        if missing and not args.estimate:
            names = ", ".join(k.upper() for k in missing)
            print("\n  " + "!" * 68)
            print(f"  ERROR: provider skipped: missing key - "
                  f"{provider.label} is not configured, 0 queries can run.")
            print(f"  Missing or empty: {names}")
            print("  Put a real value in .env (template placeholders don't"
                  " count), then retry.")
            print("  All providers and their keys: sellbird discover --providers")
            print("  " + "!" * 68 + "\n")
            raise SystemExit(1)

        config = load_config()
        state = StateManager()
        await state.init_db()

        # Semicolons, not commas: "Denver, CO" is one city, not two.
        cities = [c.strip() for c in args.city.split(";") if c.strip()] or None
        preset = getattr(args, "preset", "") or ""
        problem = preset_problem(preset, args.provider, config=config)
        if problem:
            print(f"\n  {problem}\n")
            sys.exit(2)
        queries = build_queries(config, cities=cities,
                                depth=args.depth, limit=args.limit, preset=preset)

        report = await run_discovery(state, config, args.provider, queries,
                                     max_spend=args.max_spend, dry_run=True)
        print(f"\n  {len(queries)} queries via {PROVIDERS[args.provider].label}")
        for q in queries[:8]:
            print(f"    - {q.keyword()}" + ("" if q.coordinate else
                  "   (no coordinates — add icp.geo_coordinates for radius search)"))
        if len(queries) > 8:
            print(f"    ... and {len(queries) - 8} more")
        print(f"\n  Estimated cost: ${report.estimated_cost:.4f}"
              f"   (cap: ${args.max_spend:.2f})")

        if args.estimate:
            print("\n  Estimate only. Re-run without --estimate to collect.\n")
            return

        print("\n  Running...\n")
        from sellbird.pipeline import run_prospecting

        result = await run_prospecting(
            state, config, args.provider, queries,
            max_spend=args.max_spend, profile=not args.no_profile,
        )
        r = result.discover or {}
        ran = r.get("queries_ran", 0)
        print(f"  DISCOVER — {r.get('found', 0)} results across "
              f"{ran} of {r.get('queries', 0)} queries")
        print(f"    {r.get('new_companies', 0)} new companies, "
              f"{r.get('known_companies', 0)} already known")
        print(f"    {r.get('junk', 0)} filtered out as directories/aggregators")
        # Only when it happened: a "0 skipped" line every run teaches people to
        # stop reading the block, and this is the line worth noticing.
        if r.get("excluded"):
            print(f"    {r['excluded']} skipped — on your do-not-contact list")
        print(f"    {r.get('observations', 0)} observations recorded")
        print(f"    actual cost: ${r.get('actual_cost', 0):.4f}")
        if r.get("stopped"):
            print(f"    stopped early: {r['stopped']}")

        if args.no_profile:
            print("\n  PROFILE — skipped (--no-profile)")
        else:
            print(f"\n  PROFILE — {result.profiled_companies} sites read, "
                  f"{result.profile_observations} observations  (free)")

        for err in result.errors[:5]:
            print(f"    ! {err}")

        # Empty successful searches are valid; no successful requests is not.
        # Partial success remains usable and exits zero.
        if r.get("queries_succeeded", 0) == 0:
            print("\n  " + "=" * 68)
            print(f"  DISCOVERY COMPLETED NO QUERIES ({r.get('stopped') or 'no queries succeeded'})"
                  " — exiting non-zero")
            for err in (r.get("errors") or [])[:3]:
                print(f"    - {err}")
            print("  " + "=" * 68 + "\n")
            raise SystemExit(1)

        print("\n  Next: sellbird dashboard → Signals → cohort builder\n")

    asyncio.run(_discover())


def cmd_signals(args):
    """Review the signal vocabulary — Sellbird proposes, you confirm.

    Nothing is collected until a signal is confirmed, so a fresh install
    prospects against nothing until someone makes these decisions.
    """
    from sellbird.state import StateManager
    from sellbird.signals import seed_signal_catalog

    async def _signals():
        state = StateManager()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = [a.strip().upper() for a in (args.confirm or args.reject or "").split(",")
                 if a.strip()]
        if codes:
            status = "confirmed" if args.confirm else "rejected"
            if codes == ["ALL"]:
                codes = [c["code"] for c in await state.get_signal_codes()]
            elif codes == ["FREE"]:
                # Only signals whose cost note STARTS with free/included. A
                # substring match would sweep in "1 credit (free tiers
                # available)", which is not free.
                codes = [c["code"] for c in await state.get_signal_codes()
                         if (c["cost_note"] or "").lower().startswith(("free", "included"))]
            changed = sum(
                [1 for c in codes if await state.set_signal_status(c, status)]
            )
            print(f"\n  {changed} signal(s) {status}.\n")
            return

        rows = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}
        by_status = {"confirmed": 0, "proposed": 0, "rejected": 0}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1

        print(f"\n  Signals — {by_status['confirmed']} confirmed, "
              f"{by_status['proposed']} awaiting you, {by_status['rejected']} off")
        print("  " + "=" * 66)
        mark = {"confirmed": "[on] ", "proposed": "[ ? ]", "rejected": "[off]"}
        current = None
        for r in rows:
            if r["category"] != current:
                current = r["category"]
                print(f"\n  {current.upper()}")
            seen = counts.get(r["code"], {}).get("companies", 0)
            seen_txt = f"  ({seen} companies)" if seen else ""
            print(f"    {mark.get(r['status'], '     ')} {r['code']:<22} "
                  f"{r['label']}{seen_txt}")
            print(f"          {r['cost_note']}")

        if by_status["proposed"]:
            print("\n  Confirm with: sellbird signals --confirm CODE[,CODE...]")
            print("  Everything free: sellbird signals --confirm free")
            print("  Details and descriptions: sellbird dashboard → Signals\n")
        else:
            print("")

    asyncio.run(_signals())


def cmd_retry_writes(args):
    """Put prospects the Writer gave up on back in line for another try."""
    from sellbird.agents.writer import MAX_WRITE_ATTEMPTS
    from sellbird.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        count = await state.retry_write_failed()
        if count:
            print(f"\n  {count} prospect(s) are back in line. The Writer will try each "
                  f"up to {MAX_WRITE_ATTEMPTS} more times.\n")
        else:
            print("\n  No prospects are waiting on a retry.\n")

    asyncio.run(_run())


def cmd_warmup(args):
    """Check whether the sending domain is ready for cold email."""
    import os

    from dotenv import load_dotenv

    from sellbird import deliverability as d
    from sellbird.agents.handler import BOUNCE_COUNT_KEY
    from sellbird.config import ConfigError, load_config
    from sellbird.paths import PROJECT_ROOT
    from sellbird.state import StateManager

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
    except ConfigError as e:
        print(f"\n  {e}\n")
        raise SystemExit(1)
    email = config.channels.email
    smtp_user = os.getenv("SMTP_USERNAME", "").strip() if email.provider == "smtp" else ""
    sender = smtp_user if "@" in smtp_user else config.persona.email
    domain = (args.domain or sender.rpartition("@")[2]).strip().lower().rstrip(".")
    if not d.DOMAIN_RE.match(domain):
        print("\n  No valid sending domain found. Pass one: sellbird warmup yourdomain.com\n")
        raise SystemExit(1)

    async def _history():
        state = StateManager()
        await state.init_db()
        return (await state.count_outbox_sent(),
                int(await state.get_setting(BOUNCE_COUNT_KEY) or 0))

    smtp_host = os.getenv("SMTP_HOST", "").strip() if email.provider == "smtp" else ""
    print(f"\n  Deliverability check - {domain}")
    print("  " + "=" * 56)
    sections = [
        ("Authentication", [d.check_mx(domain), d.check_spf(domain),
                            d.check_dkim(domain, email.warmup.dkim_selector),
                            d.check_dmarc(domain)]),
        ("Reputation", [d.check_domain_age(domain), *d.check_domain_blocklists(domain)]
         + (d.check_ip_blocklists(smtp_host) if smtp_host else [])),
        ("Warmup & sending", [d.check_warmup(email.warmup, email.max_daily_sends,
                                             provider=email.provider),
                              _warmup_service_check(sender, email.warmup),
                              d.check_send_history(*asyncio.run(_history()))]),
    ]
    icons = {d.OK: "OK  ", d.WARN: "WARN", d.FAIL: "FAIL", d.INFO: "info"}
    failed = warned = unknown = 0
    for title, checks in sections:
        print(f"\n  {title}")
        for c in checks:
            failed += c.status == d.FAIL
            warned += c.status == d.WARN
            unknown += c.unknown and title == "Authentication"
            print(f"    [{icons[c.status]}] {c.name:<26} {c.detail}")

    print("\n  " + "=" * 56)
    if failed:
        print(f"  NOT READY - fix the {failed} FAIL item(s) before any cold email.")
    elif unknown:
        print(f"  NOT CHECKED - {unknown} authentication lookup(s) failed; run again.")
    elif warned:
        print(f"  READY WITH CAVEATS - {warned} warning(s); keep volume low.")
    else:
        print("  READY - send within today's cap and watch bounces.")
    print("\n  What DNS can't tell you (the real reputation signal):")
    print("    - Google Postmaster Tools: https://postmaster.google.com - add the domain;")
    print("      it shows Gmail's spam rate and domain reputation once you send volume.")
    print("    - Inbox placement: send one email to a seed test (mail-tester.com is free)")
    print("      and aim for 9/10+. TrulyInbox shows the warmup inbox rate, and with it")
    print("      connected (sellbird trulyinbox connect) Sellbird reads it into this report.")
    print("      Wait for ~95%+ inbox for a week before relying on cold sends.\n")
    if failed or unknown:
        raise SystemExit(1)


def _warmup_service_check(address: str, warmup):
    """The 'Warmup service' line: TrulyInbox's view of the mailbox, or the recommendation."""
    from sellbird import deliverability as d
    from sellbird.integrations import trulyinbox as ti
    from sellbird.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        health, error = await ti.current_health(address)
        other = health is None and not error and await ti.get_choice(state) == "other"
        return d.check_warmup_service(health, warmup, error=error, other_service=other)
    return asyncio.run(_run())


def cmd_trulyinbox(args):
    """Connect TrulyInbox, record another warmup service, or show the status."""
    import getpass

    from dotenv import load_dotenv

    from sellbird import deliverability as d
    from sellbird.config import ConfigError, load_config
    from sellbird.integrations import trulyinbox as ti
    from sellbird.paths import PROJECT_ROOT
    from sellbird.state import StateManager

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
    except ConfigError as e:
        print(f"\n  {e}\n")
        raise SystemExit(1)
    address = ti.sending_address(config)
    warmup = config.channels.email.warmup

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.trulyinbox_action == "other":
            await ti.set_choice(state, "other")
            print("\n  Noted: you use another warmup service. Sellbird won't ask about "
                  "TrulyInbox again.\n")
            return 0
        if args.trulyinbox_action == "connect":
            print(f"\n  Connect TrulyInbox. Don't have it yet? {ti.AFFILIATE_URL} {ti.AFFILIATE_TAG}")
            print("  You need Starter or higher. In TrulyInbox, open Settings > API keys and")
            print("  create a workspace key with accounts:read, warmup:read, analytics:read")
            print("  and workspace:read.\n")
            if not address:
                print(f"\n  {ti.NO_ADDRESS}\n")
                return 1
            key = getpass.getpass("  TrulyInbox API key (hidden): ").strip()
            if not key:
                print("\n  No key entered.\n")
                return 1
            workspace = ti.key_from_env()[1]
            ti.forget_rejected(key)
            try:
                client = ti.TrulyInbox(key, workspace)
                health = await client.mailbox_health(address)
            except ti.TrulyInboxError as e:
                print(f"\n  {e}\n")
                return 1
            from sellbird.dashboard import _write_env_file
            updates = {"TRULYINBOX_API_KEY": key}
            if not workspace:
                updates["TRULYINBOX_WORKSPACE_ID"] = client.workspace_id
            try:
                _write_env_file(updates)
            except OSError as e:
                print(f"\n  The key works, but Sellbird couldn't save it to .env: {e}\n")
                return 1
            await ti.set_choice(state, "connected")
            print("\n  Connected. " + d.check_warmup_service(health, warmup).detail + "\n")
            return 0
        choice = await ti.get_choice(state)
        health, error = await ti.current_health(address)
        print(f"\n  TrulyInbox: {ti.describe_status(health, error, choice)}")
        check = d.check_warmup_service(health, warmup, error=error,
                                       other_service=choice == "other")
        print(f"  {check.detail}\n")
        return 0

    raise SystemExit(asyncio.run(_run()))


def cmd_precleared_key(args):
    """Get a free Precleared key for an email address and save it to .env.

    The same one-step key as `sellbird setup` offers, for after setup: when the
    keyless allowance runs out, or for a coding agent setting Sellbird up
    (`--email` asks nothing). The key is saved, never printed.
    """
    import os

    from dotenv import load_dotenv

    from sellbird.dashboard import _write_env_file
    from sellbird.integrations.precleared import GET_KEY_URL, create_free_key
    from sellbird.paths import PROJECT_ROOT
    from sellbird.setup import _read_env_file

    load_dotenv(PROJECT_ROOT / ".env")
    if (_read_env_file().get("PRECLEARED_API_KEY", "").strip("'\" ")
            or os.getenv("PRECLEARED_API_KEY", "").strip()):
        print("\n  You already have a Precleared key (PRECLEARED_API_KEY), so Sellbird kept it.")
        print("  To get a new one, delete that line from .env and run this again.\n")
        raise SystemExit(0)

    enabled, country = None, ""
    try:
        from sellbird.config import load_config
        settings = load_config().compliance.precleared
        enabled = settings.enabled is True
        country = settings.sender_country if enabled else ""
    except Exception:
        pass  # only used for a hint and the key's country

    email = (args.email or "").strip()
    if not email:
        print("\n  Sellbird can get you a free Precleared key: 100 checks a day for the")
        print("  first 14 days, then 25 a day. Precleared receives your email address")
        print("  to make the key (privacy: https://precleared.dev/privacy).")
        try:
            email = input("\n  → Email address for the key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(1)
        if not email:
            print("\n  No email address entered.\n")
            raise SystemExit(1)

    result = create_free_key(email, country=country)
    if not result.key:
        print(f"\n  Couldn't get a key: {result.error}.")
        print(f"  You can get one at {GET_KEY_URL} and put it in .env as PRECLEARED_API_KEY.\n")
        raise SystemExit(1)
    try:
        _write_env_file({"PRECLEARED_API_KEY": result.key})
    except OSError as e:
        print(f"\n  Precleared made the key, but Sellbird couldn't save it to .env ({e}).")
        print(f"  Fix that and run this again, or get a key at {GET_KEY_URL}.\n")
        raise SystemExit(1)
    print("\n  Done: your free Precleared key is saved in .env as PRECLEARED_API_KEY.")
    print("  It gives Sellbird 100 checks a day for 14 days, then 25 a day.")
    if enabled is False:
        print("  The check itself is off: turn it on with `sellbird setup` or set")
        print("  compliance.precleared.enabled: true in your config.")
    print("  If Sellbird is running, restart it so it uses the key.\n")
    raise SystemExit(0)


def cmd_sending(args):
    """Pause/resume the sending kill switch."""
    from sellbird.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.sending_action == "pause":
            await state.set_setting("sending_paused", "paused manually")
            print("\n  Sending paused. Nothing will leave the outbox.\n")
        elif args.sending_action == "resume":
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
            print("\n  Sending resumed (bounce counter reset).\n")
        else:
            paused = await state.get_setting("sending_paused")
            print(f"\n  Sending: {'PAUSED — ' + paused if paused else 'active'}\n")

    asyncio.run(_run())


def _carry_over_from_harvey():
    """Move a Harvey-era install's files to their new names and say so once."""
    import sellbird
    from sellbird.legacy import migrate_files
    notes = migrate_files()
    if sellbird.LEGACY_ENV:
        notes.append("Using your HARVEY_* settings as "
                     + ", ".join(sellbird.LEGACY_ENV)
                     + ". Rename them when convenient.")
    for note in notes:
        print(f"Sellbird (formerly Harvey): {note}", file=sys.stderr)


def cmd_suppressions(args):
    """See, add or remove suppressed addresses and domains. Removing is yours alone."""
    from sellbird.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.suppression_action == "add":
            kind = "domain" if "@" not in args.value else "address"
            try:
                added = await state.add_suppression(kind, args.value, "manual", source="cli")
            except ValueError:
                print(f"\n  That isn't an email address or a domain: {args.value}\n")
                return 1
            key = state.suppression_key(kind, args.value)
            print(f"\n  {'Suppressed' if added else 'Already suppressed'}: {key} ({kind}).\n")
        elif args.suppression_action == "remove":
            if await state.remove_suppression(args.value):
                print("\n  Removed. Sellbird may email them again.\n")
            else:
                print(f"\n  No suppression with id {args.value}. Run `sellbird suppressions` to see ids.\n")
                return 1
        else:
            rows = await state.list_suppressions(limit=1000)
            if not rows:
                print("\n  Nobody is suppressed.\n")
            for r in rows:
                print(f"  {r['id']}  {r['kind']:<7} {r['value']:<40} {r['reason']:<10} {r['created_at']}")
        return 0

    code = asyncio.run(_run())
    if code:
        sys.exit(code)


def main():
    from sellbird.textio import utf8_stdio
    utf8_stdio()
    from sellbird.paths import PROJECT_ROOT
    project_root = str(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        prog="sellbird",
        description="Sellbird — the open-source outreach platform. Make the first move.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # sellbird install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # sellbird setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # sellbird run
    sub = subparsers.add_parser("run", help="Start Sellbird's heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # sellbird train <url>
    sub = subparsers.add_parser("train", help="Train Sellbird on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    sub.add_argument(
        "--provider", choices=["claude", "codex"], default=None,
        help="Subscription to think with (default: the one in your config, else claude)",
    )
    sub.add_argument("--model", default=None, help="Model for this provider (default: provider default)")
    sub.add_argument(
        "--reasoning-effort", default=None,
        help="Codex reasoning effort (minimal/low/medium/high/xhigh)",
    )
    sub.set_defaults(func=cmd_train)

    # sellbird dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1",
                     help="Loopback address to listen on (default: 127.0.0.1; "
                          "other addresses are refused)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.add_argument("--url", action="store_true",
                     help="Print the unlock link (with its token) and exit")
    sub.set_defaults(func=cmd_dashboard)

    # sellbird status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.add_argument(
        "--check", action="store_true",
        help="Also check the configured brain now (Codex: CLI version, isolation "
             "and login; Claude: CLI isolation flags; no model call)",
    )
    sub.set_defaults(func=cmd_status)

    # sellbird export
    sub = subparsers.add_parser(
        "export", help="Export prospects as a sequencer-ready CSV"
    )
    sub.add_argument("--out", default="prospects.csv", help="Output file (default: prospects.csv)")
    sub.add_argument(
        "--email-status", default="",
        help="Comma-separated statuses to include (default: verified,risky)",
    )
    sub.add_argument("--min-score", type=int, default=0, help="Minimum ICP score")
    sub.add_argument("--status", default="", help="Comma-separated pipeline statuses (e.g. new,queued); opted-out contacts stay excluded")
    sub.add_argument("--all", action="store_true", help="Export the raw list without filters, including opted-out contacts (not for outreach)")
    sub.set_defaults(func=cmd_export)

    # sellbird companies
    sub = subparsers.add_parser(
        "companies", help="Export the company register: every company, why it fits, and why it wasn't contacted"
    )
    sub.add_argument("--out", default="", help="Output file (default: data/exports/company-register-<date>.csv)")
    sub.set_defaults(func=cmd_companies)

    # sellbird gmail auth|test
    sub = subparsers.add_parser("gmail", help="Gmail provider setup")
    sub.add_argument("gmail_action", choices=["auth", "test"],
                     help="auth: one-time OAuth; test: verify connection")
    sub.set_defaults(func=cmd_gmail)

    # sellbird microsoft auth|test
    sub = subparsers.add_parser("microsoft", help="Microsoft 365 provider setup")
    sub.add_argument("microsoft_action", choices=["auth", "test"],
                     help="auth: one-time sign-in; test: verify connection")
    sub.set_defaults(func=cmd_microsoft)

    # sellbird outbox
    sub = subparsers.add_parser("outbox", help="Review/approve queued emails")
    sub.add_argument("--approve", metavar="ID", default="", help="Approve one item")
    sub.add_argument("--approve-all", action="store_true", help="Approve all pending")
    sub.add_argument("--reject", metavar="ID", default="", help="Reject one item")
    sub.set_defaults(func=cmd_outbox)

    # sellbird sending pause|resume|status
    sub = subparsers.add_parser("discover", help="Find businesses matching your ICP")
    sub.add_argument("--provider", default="osm",
                     help="Discovery provider (default: osm — free, no account)")
    sub.add_argument("--providers", action="store_true",
                     help="List every provider with cost and setup, then exit")
    sub.add_argument("--estimate", action="store_true",
                     help="Print projected spend and exit without calling anything")
    sub.add_argument("--city", default="",
                     help='Semicolon-separated cities, e.g. "Denver, CO;Dallas, TX" '
                          '(default: icp.geography)')
    sub.add_argument("--depth", type=int, default=30,
                     help="SERP depth (default: 30 — depth 100 costs 10x since Sept 2025)")
    sub.add_argument("--limit", type=int, default=100,
                     help="Max records per query (default: 100)")
    sub.add_argument("--max-spend", type=float, default=1.0,
                     help="Hard cap in dollars (default: 1.00)")
    sub.add_argument("--preset", default="", choices=["", "yc", "hiring", "accelerator", "funding"],
                     help="A built-in search instead of your ICP's industries "
                          "(each needs a search provider like tinyfish, serper or dataforseo_serp). "
                          "yc: startups whose own site says they're YC-backed; "
                          "hiring: companies whose own careers pages list one of "
                          "your hiring_signals roles (else ICP titles); "
                          "accelerator: ones backed by Techstars, Antler, EF, SPC, 500 Global, "
                          "Neo, AI Grant, a16z speedrun or Plug and Play; "
                          "funding: companies announcing a raise on their own site in the last 18 months")
    sub.add_argument("--no-profile", action="store_true",
                     help="Don't profile what was found (profiling is free)")
    sub.set_defaults(func=cmd_discover)

    sub = subparsers.add_parser(
        "profile", help="Read discovered companies' websites (free, no Claude)")
    sub.add_argument("--limit", type=int, default=200,
                     help="Max sites to read (default: 200)")
    sub.add_argument("--stale-days", type=int, default=90,
                     help="Re-read a site after this many days (default: 90)")
    sub.set_defaults(func=cmd_profile)

    sub = subparsers.add_parser(
        "signals", help="Review/confirm which signals Sellbird prospects against")
    sub.add_argument("--confirm", metavar="CODES", default="",
                     help="Comma-separated codes to confirm ('all' or 'free' accepted)")
    sub.add_argument("--reject", metavar="CODES", default="",
                     help="Comma-separated codes to turn off")
    sub.set_defaults(func=cmd_signals)

    sub = subparsers.add_parser(
        "retry-writes", help="Retry prospects the Writer couldn't write an email for")
    sub.set_defaults(func=cmd_retry_writes)

    sub = subparsers.add_parser("sending", help="Kill switch: pause/resume sending")
    sub.add_argument("sending_action", nargs="?", default="status",
                     choices=["pause", "resume", "status"])
    sub.set_defaults(func=cmd_sending)

    sub = subparsers.add_parser(
        "warmup", help="Check if your sending domain is set up and warmed up for cold email")
    sub.add_argument("domain", nargs="?", default="",
                     help="Domain to check (default: your sending mailbox's domain)")
    sub.set_defaults(func=cmd_warmup)

    sub = subparsers.add_parser(
        "trulyinbox", help="Warmup service: connect TrulyInbox so Sellbird can read the inbox rate")
    sub.add_argument("trulyinbox_action", nargs="?", default="status",
                     choices=["status", "connect", "other"],
                     help="connect a key, 'other' if you use another warmup service, "
                          "or show the status (default)")
    sub.set_defaults(func=cmd_trulyinbox)

    sub = subparsers.add_parser(
        "precleared-key",
        help="Get a free Precleared key for the cold-email check and save it to .env")
    sub.add_argument("--email", default="",
                     help="Email address for the key (asked if not given)")
    sub.set_defaults(func=cmd_precleared_key)

    # sellbird usage
    sub = subparsers.add_parser("usage", help="Show brain usage and quota (Claude or Codex)")
    sub.add_argument("--days", type=int, default=30, help="Breakdown window (default: 30)")
    sub.set_defaults(func=cmd_usage)

    sub = subparsers.add_parser("suppressions", help="Addresses and domains Sellbird never emails")
    sub.add_argument("suppression_action", nargs="?", default="list", choices=["list", "add", "remove"])
    sub.add_argument("value", nargs="?", default="", help="add: an address or domain; remove: its id")
    sub.set_defaults(func=cmd_suppressions)

    # Commands that installed extensions add (sellbird.extensions). A name the
    # built-in CLI already uses is refused, so an extension never hides one.
    from sellbird import extensions
    for name, (help_text, configure) in extensions.registry().cli_commands.items():
        if name in subparsers.choices:
            raise extensions.ExtensionError(
                f"an extension's command {name!r} clashes with a built-in command")
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(func=configure(sub))

    args = parser.parse_args()
    _carry_over_from_harvey()
    args._project_root = project_root

    # Fill the home folder before any command. Silent: a command's stdout may
    # be a protocol or a file. A failure is one stderr line, and the command
    # still runs where it can.
    from sellbird import home
    from sellbird.paths import PROJECT_ROOT as home_root
    try:
        home.seed_home(home_root, home.bundled_defaults())
    except Exception as e:
        reason = (e.strerror if isinstance(e, OSError) else None) or type(e).__name__
        print(f"  Could not prepare the home folder {home_root}: {reason}", file=sys.stderr)

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    sellbird install   — Install dependencies")
        print("    sellbird setup     — Configure Sellbird (first time)")
        print("    sellbird run       — Start closing deals")
        print("    sellbird dashboard — Open the web dashboard")
        print()
        sys.exit(0)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted. Goodbye.")
        sys.exit(130)
    except Exception as e:
        # ConfigError and friends carry actionable messages — show them
        # cleanly instead of a raw traceback.
        from sellbird.config import ConfigError

        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'sellbird {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
