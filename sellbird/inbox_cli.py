"""`sellbird inboxes`: connect and run the extra inboxes you send from.

Nothing here prints a token or password. SMTP passwords are asked for with
getpass, and there is deliberately no password argument: it would land in
shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from datetime import date

from sellbird.mailboxes import (
    HARD_MAX_INBOX_CAP, MAIN, ConnectRefused, MailboxStore, build_provider,
    check_provider_allows_inboxes, connect_oauth_mailbox, connect_smtp_mailbox, load_inboxes,
)
from sellbird.secrets import SecretStore, SecretStoreUnavailable

MAIN_NAME = "main"   # how the main mailbox is named on the command line


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("use a real date, YYYY-MM-DD") from None


def _cap(value: str):
    if value == "default":
        return None
    try:
        cap = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("a whole number, or 'default'") from None
    if not 1 <= cap <= HARD_MAX_INBOX_CAP:
        raise argparse.ArgumentTypeError(f"between 1 and {HARD_MAX_INBOX_CAP}")
    return cap


def register(subparsers) -> None:
    sub = subparsers.add_parser("inboxes", help="Connect and run the inboxes you send from")
    actions = sub.add_subparsers(dest="inboxes_action")

    actions.add_parser("list", help="Every inbox, its state and today's cold sends")

    connect = actions.add_parser("connect", help="Connect an inbox you already have")
    kinds = connect.add_subparsers(dest="kind", required=True)
    for kind in ("gmail", "microsoft", "smtp"):
        p = kinds.add_parser(kind)
        p.add_argument("--address", required=True, help="The inbox's email address")
        p.add_argument("--name", default="", help="Name shown in From (default: persona.name)")
        p.add_argument("--warmup-started", default="", type=_date, metavar="YYYY-MM-DD",
                       help="When this inbox started warming up (leave out if established)")
        if kind == "smtp":
            p.add_argument("--smtp-host", required=True)
            p.add_argument("--smtp-port", type=int, default=587)
            p.add_argument("--username", default="", help="Defaults to the address")
            p.add_argument("--imap-host", default="", help="Defaults to the SMTP host")
            p.add_argument("--imap-port", type=int, default=993)

    test = actions.add_parser("test", help="Check one inbox can sign in")
    test.add_argument("address")

    pause = actions.add_parser("pause", help="Stop sending from one inbox")
    pause.add_argument("address")
    pause.add_argument("--reason", default="paused by you")
    resume = actions.add_parser("resume", help="Send from a paused inbox again")
    resume.add_argument("address")

    cap = actions.add_parser("cap", help="Set one inbox's daily cold-email cap")
    cap.add_argument("address")
    cap.add_argument("cap", type=_cap, help=f"1-{HARD_MAX_INBOX_CAP}, or 'default'")

    name = actions.add_parser("name", help="Change the name an inbox sends as")
    name.add_argument("address")
    name.add_argument("name")

    remove = actions.add_parser(
        "remove", help="Stop using an inbox for good (its sign-in stays in your keychain)")
    remove.add_argument("address")

    use = actions.add_parser(
        "use", help="Choose which inboxes a campaign sends from (none: every inbox)")
    use.add_argument("campaign", help="Campaign id or exact name")
    use.add_argument("addresses", nargs="*",
                     help=f"Inbox addresses; '{MAIN_NAME}' is your main mailbox")
    sub.set_defaults(func=cmd_inboxes)


async def print_inbox_list(store: MailboxStore, config, env) -> None:
    from sellbird.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider

    main = get_mail_provider(config, env) \
        if config.channels.email.provider in NATIVE_PROVIDERS else None
    inboxes = await load_inboxes(store, config, env, main)
    if not inboxes.inboxes:
        print("\n  No inboxes: your sending provider is "
              f"{config.channels.email.provider or 'not set'}.\n")
        return
    print(f"\n  {'Inbox':38} {'State':16} Cold today")
    for inbox in inboxes.inboxes.values():
        label = f"{inbox.address or '(main mailbox)'}{' (main)' if inbox.main and inbox.address else ''}"
        if not inbox.connected:
            state = "not connected"
        elif inbox.paused_reason:
            state = "paused"
        else:
            state = "sending"
        print(f"  {label:38} {state:16} {inbox.sent_today}/{inbox.cap}")
        if inbox.paused_reason:
            print(f"  {'':38} {inbox.paused_reason}")
    if not inboxes.several:
        print("\n  Add another with: sellbird inboxes connect gmail|microsoft|smtp --address ...")
    print(f"\n  Everything sent today stays under max_daily_sends "
          f"({config.channels.email.max_daily_sends}).\n")


async def _test_and_print(provider) -> bool:
    try:
        ok, detail = await provider.test_connection()
    except Exception as e:
        ok, detail = False, f"Couldn't check the connection ({type(e).__name__})"
    print(f"\n  {'✓' if ok else '✗'} {detail}\n")
    return ok


def _oauth_sign_in(args, env) -> dict | None:
    """Run the browser sign-in; the token stays in memory until it's stored."""
    captured: dict = {}
    if args.kind == "gmail":
        from sellbird.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret,
                           save=captured.update, expected_address=args.address)
    else:
        from sellbird.integrations.microsoft import run_auth_flow
        ok = run_auth_flow(env.m365_client_id, env.m365_tenant_id,
                           save=captured.update, expected_address=args.address)
    return captured if ok and captured else None


def read_smtp_password(args) -> str:
    return getpass.getpass(f"SMTP password for {args.address}: ")


async def connect_flow(args, store: MailboxStore, secrets: SecretStore, config, env) -> int:
    from sellbird.integrations.smtp_mail import SmtpSettings

    try:
        check_provider_allows_inboxes(config)
        common = dict(address=args.address, display_name=args.name,
                      warmup_started_at=args.warmup_started or "")
        if args.kind == "smtp":
            settings = SmtpSettings(
                host=args.smtp_host, username=args.username or args.address.strip().lower(),
                password=read_smtp_password(args), port=args.smtp_port,
                imap_host=args.imap_host, imap_port=args.imap_port)
            mailbox_id = await connect_smtp_mailbox(store, secrets, config, settings=settings,
                                                    **common)
        else:
            token = _oauth_sign_in(args, env)
            if token is None:
                print("\n  Sign-in didn't finish, so nothing was connected.\n")
                return 1
            provider = "google" if args.kind == "gmail" else "microsoft"
            mailbox_id = await connect_oauth_mailbox(store, secrets, config, provider=provider,
                                                     token=token, **common)
    except (ConnectRefused, SecretStoreUnavailable) as e:
        print(f"\n  {e}\n")
        return 1

    print(f"\n  Connected {args.address.strip().lower()}.")
    if config.channels.email.max_daily_sends <= config.channels.email.per_inbox_daily_cap:
        print("  Note: max_daily_sends caps everything sent in a day across every inbox. "
              "Raise it in sellbird.yaml so this inbox adds to what goes out.")
    row = await store.get_mailbox(mailbox_id)
    provider = build_provider(row, config=config, env=env, secrets=secrets)
    if provider is None:
        print("  Couldn't build a connection to test.\n")
        return 1
    return 0 if await _test_and_print(provider) else 1


async def _find(store: MailboxStore, address: str) -> dict | None:
    row = await store.get_mailbox_by_address(address)
    if row is None or row.get("state") == "retired":
        print(f"\n  No connected inbox {address.strip().lower()}. See: sellbird inboxes list\n")
        return None
    return row


async def _find_campaign(store: MailboxStore, ref: str) -> str | None:
    rows = await store._rows("SELECT id, name FROM campaigns WHERE id = ? OR name = ?",
                             (ref, ref))
    if len(rows) != 1:
        print(f"\n  {'No' if not rows else 'More than one'} campaign matches {ref!r}. "
              "Use its id (sellbird status lists campaigns).\n")
        return None
    return rows[0]["id"]


async def run(args, store: MailboxStore, secrets: SecretStore, config, env) -> int:
    action = args.inboxes_action or "list"
    if action == "list":
        await print_inbox_list(store, config, env)
        return 0
    if action == "connect":
        return await connect_flow(args, store, secrets, config, env)
    if action == "use":
        campaign_id = await _find_campaign(store, args.campaign)
        if campaign_id is None:
            return 1
        ids = []
        for address in args.addresses:
            if address.strip().lower() == MAIN_NAME:
                ids.append(MAIN)
                continue
            row = await _find(store, address)
            if row is None:
                return 1
            ids.append(row["id"])
        await store.set_campaign_pool(campaign_id, ids)
        print("\n  " + ("This campaign sends from every inbox." if not ids else
                        f"This campaign sends from {len(ids)} inbox(es) only.")
              + " New threads follow this; threads already started keep their inbox.\n")
        return 0

    row = await _find(store, args.address)
    if row is None:
        return 1
    address = row["address"]
    if action == "test":
        provider = build_provider(row, config=config, env=env, secrets=secrets)
        if provider is None:
            print("\n  ✗ This inbox has no connection to test.\n")
            return 1
        return 0 if await _test_and_print(provider) else 1
    if action == "pause":
        await store.set_pause(row["id"], args.reason)
        await store.log_action(action_type="inbox_paused", agent="you",
                               details={"inbox": address, "reason": args.reason})
        print(f"\n  Paused {address}. Its unsent first emails move to your other inboxes; "
              "follow-ups and replies on threads it started wait for it.\n")
    elif action == "resume":
        await store.set_pause(row["id"], "")
        await store.log_action(action_type="inbox_resumed", agent="you",
                               details={"inbox": address})
        print(f"\n  {address} sends again.\n")
    elif action == "cap":
        await store.set_cap(row["id"], args.cap)
        await store.log_action(action_type="inbox_cap", agent="you",
                               details={"inbox": address, "cap": args.cap})
        print(f"\n  {address}: " + (f"up to {args.cap} cold emails a day." if args.cap else
                                     "back to the default daily cap and its warmup ramp.") + "\n")
    elif action == "name":
        await store.set_display_name(row["id"], args.name)
        print(f"\n  {address} sends as {args.name.strip() or config.persona.name}.\n")
    elif action == "remove":
        await store.retire(row["id"])
        await store.log_action(action_type="inbox_removed", agent="you",
                               details={"inbox": address})
        print(f"\n  Removed {address}. Its unsent first emails move to your other inboxes; "
              "follow-ups on threads it started are cancelled. Its sign-in is still in "
              "your keychain (service 'sellbird'); delete it there if you like.\n")
    return 0


def cmd_inboxes(args) -> None:
    from sellbird.config import load_config, load_env
    from sellbird.state import StateManager

    config, env = load_config(), load_env()

    async def _run() -> int:
        state = StateManager()
        await state.init_db()
        return await run(args, MailboxStore(db_path=state.db_path), SecretStore(), config, env)

    sys.exit(asyncio.run(_run()))
