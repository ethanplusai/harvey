"""Interactive setup wizard — walks you through everything Harvey needs."""

import asyncio
import getpass
import logging
import os
import shutil
from pathlib import Path

import yaml

from pydantic import ValidationError

from harvey.brain import Brain
from harvey.config import BrainConfig, normalize_prospect_country
from harvey.state import StateManager

logger = logging.getLogger("harvey.setup")

from harvey.paths import PROJECT_ROOT
from harvey.textio import read_user_text
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
CONFIG_FILE = PROJECT_ROOT / "harvey.yaml"


def _print_harvey(message: str):
    """Print a message as Harvey."""
    print(f"\n  Harvey: {message}")


def _print_step(step: int, total: int, title: str):
    """Print a step header."""
    print(f"\n{'─'*60}")
    print(f"  Step {step}/{total}: {title}")
    print(f"{'─'*60}")


def _ask(prompt: str, default: str = "", required: bool = True, secret: bool = False) -> str:
    """Ask the user a question. secret=True hides input (passwords, API keys)."""
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    while True:
        try:
            answer = reader(f"\n  → {prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  Setup cancelled.")
            raise SystemExit(1)

        if not answer and default:
            return default
        if not answer and required:
            print("    (required — please enter a value)")
            continue
        return answer


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        answer = input(f"\n  → {prompt}{suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Setup cancelled.")
        raise SystemExit(1)

    if not answer:
        return default
    return answer in ("y", "yes")


def _read_env_file() -> dict[str, str]:
    """The KEY=value pairs already in .env (comments and blank lines skipped)."""
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in read_user_text(ENV_FILE).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()
    return existing


def _config_file() -> Path:
    """The config Harvey loads: harvey.local.yaml (what `harvey train` writes)
    wins over harvey.yaml when it exists, as in config._find_config_file.
    Setup reads and writes this file, so its answers are the ones Harvey uses."""
    local = CONFIG_FILE.with_name("harvey.local.yaml")
    return local if local.exists() else CONFIG_FILE


def _current_precleared() -> dict:
    """compliance.precleared in the file setup writes to, or {} if there is none."""
    try:
        data = yaml.safe_load(read_user_text(_config_file()))
    except (OSError, yaml.YAMLError):
        return {}
    compliance = data.get("compliance") if isinstance(data, dict) else None
    section = compliance.get("precleared") if isinstance(compliance, dict) else None
    return section if isinstance(section, dict) else {}


def _save_precleared(config: dict, answers: dict) -> None:
    """Lay the setup answers over config's compliance.precleared, keeping the rest."""
    compliance = config.get("compliance")
    if not isinstance(compliance, dict):
        compliance = config["compliance"] = {}
    section = compliance.get("precleared")
    if not isinstance(section, dict):
        section = compliance["precleared"] = {}
    section.update(answers)


def _check_claude_cli() -> bool:
    """Check if Claude CLI is installed and accessible."""
    return shutil.which("claude") is not None


def _check_codex_cli() -> bool:
    """Check if Codex CLI is installed and accessible."""
    return shutil.which("codex") is not None


async def _test_claude(brain: Brain) -> bool:
    """Test that the configured brain answers (Claude or Codex)."""
    response = await brain.think(
        "Respond with exactly: HARVEY_READY",
        session_id="harvey-setup-test",
    )
    return "HARVEY_READY" in response


def _ask_brain_config() -> BrainConfig:
    """Ask which subscription Harvey thinks with. Claude is the default."""
    _print_harvey("I think with an AI subscription you already pay for:")
    print("    claude — your Claude plan, via Claude Code (default)")
    print("    codex  — your ChatGPT plan, via the Codex CLI")
    while True:
        provider = _ask("Which one? (claude/codex)", default="claude").lower()
        if provider == "codex":
            from harvey.codex import CodexError, require_supported_platform
            try:
                require_supported_platform()
            except CodexError as e:
                print(f"    ({e})")
                continue
        if provider in ("claude", "codex"):
            break
        print("    (type claude or codex)")
    if provider == "codex":
        from harvey.codex import SUGGESTED_MODEL
        print(f"    {SUGGESTED_MODEL} is the Codex model Harvey has verified as text-only")
        print("    (no code-execution tools). Press Enter to use it, or type another.")
    while True:
        if provider == "codex":
            model = _ask("Model", default=SUGGESTED_MODEL, required=False)
        else:
            model = _ask("Model (leave blank for the default)", required=False)
        try:
            return BrainConfig(provider=provider, model=model)
        except ValidationError:
            print("    (that doesn't look like a model name — try again or leave blank)")


async def _test_instantly(api_key: str) -> bool:
    """Test the Instantly API key."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            return resp.status_code == 200
    except Exception:
        return False


class SetupWizard:
    def __init__(self):
        self.env_vars: dict[str, str] = {}
        self.config: dict = {}
        self.brain_config = BrainConfig()
        # compliance.precleared answers to save; None leaves the config alone.
        self.precleared: dict | None = None
        # Whether _write_config saved the config, for the summary.
        self.config_saved = False

    async def run(self):
        """Run the full interactive setup."""
        print(f"""
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   Harvey Setup Wizard                                    ║
║   Let's get you closing deals.                           ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝""")

        _print_harvey("Hey. I'm Harvey. Let's get me set up so I can start")
        _print_harvey("closing deals for you. I'll walk you through everything.")
        _print_harvey("This takes about 5 minutes.\n")

        total_steps = 7

        # Step 1: Check prerequisites
        _print_step(1, total_steps, "Checking Prerequisites")
        await self._check_prerequisites()

        # Step 2: Connect email platform
        _print_step(2, total_steps, "Email Platform (Instantly)")
        await self._setup_email_platform()

        # Step 3: LinkedIn (optional)
        _print_step(3, total_steps, "LinkedIn (Optional)")
        self._setup_linkedin()

        # Step 4: Cloudflare crawling (optional)
        _print_step(4, total_steps, "Website Crawling (Optional)")
        self._setup_cloudflare()

        # Step 5: Train on product
        _print_step(5, total_steps, "Train Harvey on Your Product")
        await self._setup_product()

        # Step 6: Configure behavior
        _print_step(6, total_steps, "Configure Behavior")
        self._setup_behavior()

        # Step 7: Cold-email check (optional)
        _print_step(7, total_steps, "Cold-Email Check (Optional)")
        self._setup_compliance()

        # Write everything
        self._write_env()
        self._write_config()

        # Final summary
        await self._print_summary()

    async def _check_prerequisites(self):
        """Pick the brain provider and verify its CLI is installed and working."""
        self.brain_config = _ask_brain_config()
        if self.brain_config.provider == "codex":
            await self._check_codex()
        else:
            await self._check_claude()

    async def _check_claude(self):
        # Check Claude CLI
        print("\n  Checking Claude Code CLI...", end=" ")
        if _check_claude_cli():
            print("✓ Found")
        else:
            print("✗ Not found")
            _print_harvey("I need Claude Code CLI to think. Install it from:")
            print("         https://claude.ai/download")
            print("\n         After installing, run 'claude login' to authenticate,")
            print("         then re-run this setup.")
            raise SystemExit(1)

        # Test headless mode
        print("  Testing Claude headless mode...", end=" ", flush=True)
        state = StateManager()
        await state.init_db()
        brain = Brain(state, self.brain_config)
        if await _test_claude(brain):
            print("✓ Working")
        else:
            print("✗ Failed")
            _print_harvey("Claude CLI is installed but headless mode isn't working.")
            _print_harvey("Make sure you're logged in: run 'claude login'")
            print("\n         Also verify your Max subscription is active.")
            if not _ask_yes_no("Continue anyway?", default=False):
                raise SystemExit(1)
        self._check_python_packages()

    async def _check_codex(self):
        from harvey.codex import (
            CODEX_INSTALL_HINT, CodexError, check_cli_version, check_native_auth,
        )

        print("\n  Checking Codex CLI...", end=" ")
        if _check_codex_cli():
            print("✓ Found")
        else:
            print("✗ Not found")
            _print_harvey("I need the Codex CLI to think with your ChatGPT plan:")
            print(f"         {CODEX_INSTALL_HINT}")
            print("\n         Then run 'codex login', choose 'Sign in with ChatGPT',")
            print("         and re-run this setup.")
            raise SystemExit(1)

        print("  Checking Codex CLI version...", end=" ")
        try:
            print(f"✓ {check_cli_version()}")
        except CodexError as e:
            print("✗ Not supported")
            _print_harvey(str(e))
            raise SystemExit(1)

        print("  Checking Codex ChatGPT login...", end=" ")
        try:
            check_native_auth()
            print("✓ Found")
        except CodexError as e:
            print("✗ Not ready")
            _print_harvey(str(e))
            raise SystemExit(1)

        # A real (subscription, not API-billed) call through the same
        # isolated runtime Harvey uses for every prompt.
        print("  Testing Codex (isolated, no tools)...", end=" ", flush=True)
        state = StateManager()
        await state.init_db()
        brain = Brain(state, self.brain_config)
        if await _test_claude(brain):
            print("✓ Working")
        else:
            print("✗ Failed")
            _print_harvey("Codex is installed but the test call failed. The log")
            _print_harvey("above says why (login, plan limits, or isolation).")
            if not _ask_yes_no("Continue anyway?", default=False):
                raise SystemExit(1)
        self._check_python_packages()

    def _check_python_packages(self):
        # Check Python packages
        print("  Checking Python dependencies...", end=" ")
        missing = []
        for pkg in ["httpx", "aiosqlite", "yaml", "pydantic", "bs4"]:
            try:
                __import__(pkg)
            except ImportError:
                missing.append(pkg)
        if not missing:
            print("✓ All installed")
        else:
            print(f"✗ Missing: {', '.join(missing)}")
            print("\n  Run: pip install -r requirements.txt")
            if not _ask_yes_no("Continue anyway?", default=False):
                raise SystemExit(1)

        _print_harvey("Prerequisites look good. Let's keep going.")

    async def _setup_email_platform(self):
        """Configure Instantly API."""
        _print_harvey("I need a cold email platform to send campaigns through.")
        _print_harvey("Right now I support Instantly (recommended) and Woodpecker.")

        has_instantly = _ask_yes_no("Do you have an Instantly account?")

        if has_instantly:
            api_key = _ask("Instantly API key (from Settings → Integrations; input hidden)", secret=True)
            self.env_vars["INSTANTLY_API_KEY"] = api_key

            # Test the key
            print("\n  Testing API key...", end=" ", flush=True)
            if await _test_instantly(api_key):
                print("✓ Connected")
                _print_harvey("Perfect. I can send campaigns through Instantly now.")
            else:
                print("✗ Failed")
                _print_harvey("Couldn't connect. The key might be wrong, or you might")
                _print_harvey("need the Growth plan for API access. I'll save it anyway —")
                _print_harvey("you can fix it later in .env.")
        else:
            _print_harvey("No problem. You'll need one eventually to send emails.")
            _print_harvey("Sign up at https://instantly.ai (Growth plan for API access).")
            _print_harvey("Add your API key to .env later as INSTANTLY_API_KEY.")
            self.env_vars["INSTANTLY_API_KEY"] = ""

    def _setup_linkedin(self):
        """Configure LinkedIn credentials."""
        _print_harvey("I can search LinkedIn to find prospects matching your ICP.")
        _print_harvey("I'll log into your LinkedIn account using a browser, with")
        _print_harvey("conservative rate limits and human-like pacing.")
        print("\n  ⚠  Heads up: automating LinkedIn violates their Terms of Service.")
        print("     Accounts doing this can be restricted or banned. Only use an")
        print("     account you're comfortable putting at risk, and keep the")
        print("     default rate limits. This is entirely optional.")

        use_linkedin = _ask_yes_no("Set up LinkedIn prospecting anyway?", default=False)

        if use_linkedin:
            email = _ask("LinkedIn email/username")
            password = _ask("LinkedIn password (input hidden)", secret=True)
            self.env_vars["LINKEDIN_EMAIL"] = email
            self.env_vars["LINKEDIN_PASSWORD"] = password

            _print_harvey("Got it. I'll stay well under the rate limits and behave")
            _print_harvey("like a careful human, but the ToS risk is yours to own.")
        else:
            _print_harvey("That's fine. I can still find prospects via Google")
            _print_harvey("and company websites. You can add LinkedIn later.")
            self.env_vars["LINKEDIN_EMAIL"] = ""
            self.env_vars["LINKEDIN_PASSWORD"] = ""

    def _setup_cloudflare(self):
        """Configure Cloudflare Browser Rendering for deep crawling."""
        _print_harvey("When I train on a product website, I can crawl it deeply")
        _print_harvey("using Cloudflare's Browser Rendering API. This means I can")
        _print_harvey("handle JavaScript-heavy sites and crawl hundreds of pages.")
        _print_harvey("It's $5/month for ~12,000 pages. Totally optional.")

        use_cloudflare = _ask_yes_no("Set up Cloudflare crawling?", default=False)

        if use_cloudflare:
            account_id = _ask("Cloudflare Account ID")
            api_token = _ask("Cloudflare API Token (needs Browser Rendering - Edit permission; input hidden)", secret=True)
            self.env_vars["CLOUDFLARE_ACCOUNT_ID"] = account_id
            self.env_vars["CLOUDFLARE_API_TOKEN"] = api_token
            _print_harvey("Nice. I'll use Cloudflare for deep crawling during training.")
        else:
            _print_harvey("No problem. I'll use my built-in crawler instead. It works")
            _print_harvey("great for most sites — just can't render JavaScript.")
            self.env_vars["CLOUDFLARE_ACCOUNT_ID"] = ""
            self.env_vars["CLOUDFLARE_API_TOKEN"] = ""

    async def _setup_product(self):
        """Train Harvey on the product — either via URL or manual entry."""
        _print_harvey("Now the important part — I need to learn about what you're selling.")
        _print_harvey("I can either crawl your website and figure it out myself,")
        _print_harvey("or you can tell me the basics manually.\n")

        method = _ask(
            "Train from website URL or enter manually? (url/manual)",
            default="url",
        ).lower()

        if method in ("url", "u", "website"):
            url = _ask("Your product's website URL (e.g. https://yourcompany.com)")

            _print_harvey(f"Give me a minute — I'm going to crawl {url}")
            _print_harvey("and learn everything I can about your product.\n")

            from harvey.trainer import Trainer
            trainer = Trainer(brain_config=self.brain_config)
            self.config = await trainer.train(url, str(_config_file())) or {}

            if self.config:
                _print_harvey("Training complete! I've got a solid understanding")
                _print_harvey("of your product now. Check harvey.yaml to review")
                _print_harvey("what I learned — tweak anything that looks off.")
                return

            _print_harvey("Hmm, had trouble with that URL. Let's do it manually.")

        # Manual entry
        _print_harvey("I'll ask you a few questions about your product.\n")

        company = _ask("Company name")
        product = _ask("Product/service name")
        description = _ask("One-line description (what does it do, who is it for?)")
        pricing = _ask("Pricing info", default="Contact for pricing")

        print("\n  Enter 3-5 key benefits (one per line, empty line to finish):")
        benefits = []
        while True:
            b = _ask(f"  Benefit {len(benefits) + 1}", required=False)
            if not b:
                break
            benefits.append(b)

        _print_harvey("Now let's define your ideal customer.\n")

        industries = _ask("Target industries (comma-separated)").split(",")
        industries = [i.strip() for i in industries if i.strip()]

        titles = _ask("Decision-maker titles (comma-separated)").split(",")
        titles = [t.strip() for t in titles if t.strip()]

        company_size = _ask("Target company size", default="10-200 employees")
        geography = _ask(
            "Target geography (for a city, include the state/country, "
            "e.g. 'Solon, Ohio' — an unqualified city name can silently "
            "geocode to the wrong place)",
            default="United States",
        ).split(",")
        geography = [g.strip() for g in geography if g.strip()]

        _print_harvey("Now let's talk about your offer — what happens when someone's interested?\n")

        primary_offer = _ask(
            "What's your main offer? (e.g., 'SaaS subscription at $99/mo', 'Marketing retainer')",
            required=False,
        )
        entry_offer = _ask(
            "Any low-commitment entry offer? (e.g., 'Free trial', 'Free audit', 'Sample report')",
            default="",
            required=False,
        )

        print("\n  What's the goal when someone shows interest?")
        print("    1. Book a call")
        print("    2. Start a free trial")
        print("    3. Just get a reply / start a conversation")
        goal_choice = _ask("Goal (1/2/3)", default="1")
        goal_map = {"1": "book_call", "2": "start_trial", "3": "get_reply"}
        goal = goal_map.get(goal_choice, "book_call")

        booking_method = "suggest_times"
        booking_url = ""
        meeting_duration = "15 minutes"
        meeting_owner = ""

        if goal == "book_call":
            has_calendar = _ask_yes_no("Do you have a calendar booking link (Calendly, Cal.com, etc.)?")
            if has_calendar:
                booking_url = _ask("Booking URL")
                booking_method = "calendar_link"
            else:
                print("\n  How should Harvey suggest meeting times?")
                print("    1. Suggest specific times ('How about Thursday at 2pm?')")
                print("    2. Ask for their preference ('What does your calendar look like?')")
                method_choice = _ask("Method (1/2)", default="1")
                booking_method = "suggest_times" if method_choice == "1" else "ask_preference"

            meeting_duration = _ask("How long is the call?", default="15 minutes")
            meeting_owner = _ask("Who takes the meeting? (your name or role)", required=False)

        _print_harvey("What persona should I use for outreach?\n")

        persona_name = _ask("My name (what prospects see)", default="Harvey")
        persona_email = _ask("My email address")
        persona_role = _ask("My title/role", default="Business Development")

        self.config = {
            "persona": {
                "name": persona_name,
                "company": company,
                "role": persona_role,
                "email": persona_email,
                "linkedin": "",
                "tone": "professional, consultative, confident",
            },
            "product": {
                "name": product,
                "description": description,
                "pricing": pricing,
                "key_benefits": benefits or ["Benefit 1"],
                "objection_responses": {},
                "offer": {
                    "primary": primary_offer or "",
                    "entry": entry_offer or "",
                    "goal": goal,
                    "booking_method": booking_method,
                    "booking_url": booking_url,
                    "meeting_duration": meeting_duration,
                    "meeting_owner": meeting_owner or "",
                },
            },
            "icp": {
                "industries": industries or ["Technology"],
                "company_size": company_size,
                "titles": titles or ["VP", "Director"],
                "geography": geography,
            },
            "channels": {
                "email": {
                    "enabled": True,
                    "provider": "instantly",
                    "max_daily_sends": 50,
                },
                "linkedin": {
                    "enabled": bool(self.env_vars.get("LINKEDIN_EMAIL")),
                    "max_daily_connections": 20,
                    "max_daily_messages": 10,
                },
            },
            "usage": {
                "max_daily_claude_percent": 80,
                "heartbeat_interval_minutes": 15,
                "quiet_hours": {
                    "start": "22:00",
                    "end": "07:00",
                    "timezone": "America/New_York",
                },
            },
        }

        _print_harvey("Got it. I have everything I need about your product.")

    def _setup_behavior(self):
        """Configure Harvey's operational behavior."""
        _print_harvey("Last thing — a few settings for how I operate.\n")

        # Usage limit
        print("  I track my own Claude usage so I don't eat your whole daily quota.")
        usage_pct = _ask(
            "Max % of daily Claude usage I can consume",
            default="80",
        )
        try:
            usage_pct = min(100.0, max(1.0, float(usage_pct)))
        except ValueError:
            print("    (didn't catch that — using 80)")
            usage_pct = 80.0

        # Heartbeat interval
        print("\n  I wake up on a schedule to check for work.")
        interval = _ask(
            "How often should I check for work (minutes)?",
            default="15",
        )
        try:
            interval = max(1, int(interval))
        except ValueError:
            print("    (didn't catch that — using 15)")
            interval = 15

        # Quiet hours
        print("\n  I won't send emails or do outreach during quiet hours.")
        quiet_start = _ask("Quiet hours start (24h format)", default="22:00")
        quiet_end = _ask("Quiet hours end (24h format)", default="07:00")
        timezone = _ask("Your timezone", default="America/New_York")

        # Daily send limit
        daily_sends = _ask(
            "Max emails to send per day",
            default="50",
        )
        try:
            daily_sends = max(1, int(daily_sends))
        except ValueError:
            print("    (didn't catch that — using 50)")
            daily_sends = 50
        if daily_sends > 200:
            print("\n  ⚠  200+ sends/day from a fresh domain will torch your")
            print("     deliverability. Warm up gradually (see README).")

        # Update config
        if self.config:
            self.config["usage"] = {
                "max_daily_claude_percent": usage_pct,
                "heartbeat_interval_minutes": interval,
                "quiet_hours": {
                    "start": quiet_start,
                    "end": quiet_end,
                    "timezone": timezone,
                },
            }
            if "channels" in self.config and "email" in self.config["channels"]:
                self.config["channels"]["email"]["max_daily_sends"] = daily_sends

        _print_harvey("Perfect. All configured.")

    def _setup_compliance(self):
        """Offer the optional per-recipient check through Precleared.

        Off unless the user says yes. Re-running setup offers the saved
        answers as the defaults, so pressing Enter never switches it off.
        """
        current = _current_precleared()
        was_on = current.get("enabled") is True
        _print_harvey("Before the first email to anyone new, I can ask Precleared")
        _print_harvey("(precleared.dev) which cold-email rules apply to them. A UK")
        _print_harvey("sole trader needs consent where a limited company doesn't,")
        _print_harvey("and the address alone won't tell you which. Totally optional.")
        print("\n  Anything that isn't a clear yes waits in the Outbox with the reason.")
        print("  It checks mail sent through Gmail, Microsoft 365 or SMTP, not Instantly.")
        print("  Precleared receives each new recipient's address, your country, how")
        print("  the address was found, your product description and the recipient's")
        print("  country when the company's location makes it certain, else your")
        print("  prospects' country if you set one. It also gets the company's and")
        print("  person's names, to find a UK company on the register; it doesn't")
        print("  store the person's name.")

        if not _ask_yes_no("Turn on the cold-email check?", default=was_on):
            # Only touch the config to switch off a check that was on.
            self.precleared = {"enabled": False} if was_on else None
            _print_harvey("No problem. You can turn it on later under")
            _print_harvey(f"compliance.precleared in {_config_file().name}.")
            return

        saved_country = str(current.get("sender_country") or "").strip().upper()
        if saved_country not in ("GB", "US"):
            saved_country = ""
        while True:
            country = _ask(
                "Where is your business based? (GB or US"
                + (")" if saved_country else "; blank to skip)"),
                default=saved_country,
                required=False,
            ).upper()
            country = {"UK": "GB", "USA": "US"}.get(country, country)
            if country in ("GB", "US"):
                break
            if not country:
                self.precleared = {"enabled": False} if was_on else None
                _print_harvey("Precleared needs a sender based in the UK or US,")
                _print_harvey("so I'll leave the check off for now.")
                return
            print("    (type GB or US, or leave it blank to skip)")

        try:
            saved_prospects = normalize_prospect_country(current.get("prospect_country"))
        except ValueError:
            saved_prospects = ""
        hint = "type none if not" if saved_prospects else "blank if not"
        while True:
            answer = _ask(
                f"Your prospects' country, if they're all in one (e.g. US; {hint})",
                default=saved_prospects,
                required=False,
            )
            if answer.strip().lower() == "none":
                prospects = ""
                break
            # "no" answers "if they're all in one" but is also Norway's code.
            if answer.strip().lower() == "no" and not _ask_yes_no(
                    "NO is Norway's code. Are your prospects all in Norway?", default=False):
                prospects = ""
                break
            try:
                prospects = normalize_prospect_country(answer)
                break
            except ValueError as e:
                print(f"    ({e})")

        self.precleared = {
            "enabled": True,
            "sender_country": country,
            "prospect_country": prospects,
        }
        self._ask_precleared_key()

    def _ask_precleared_key(self):
        """Offer a free Precleared key. Enter keeps the current one, or skips."""
        saved_key = _read_env_file().get("PRECLEARED_API_KEY", "").strip("'\" ")
        has_key = bool(saved_key or os.getenv("PRECLEARED_API_KEY", "").strip())
        if not has_key:
            _print_harvey("Without a key Precleared allows 10 checks a day. A free key")
            _print_harvey("from https://precleared.dev/get-key allows 25.")
        key = _ask(
            "Precleared API key (input hidden; Enter keeps your current key)" if has_key
            else "Precleared API key (input hidden; Enter to skip)",
            required=False,
            secret=True,
        )
        if key:
            self.env_vars["PRECLEARED_API_KEY"] = key
            _print_harvey("Saved the key to .env.")
        elif has_key:
            _print_harvey("Keeping the key you already have.")
        else:
            self.env_vars["PRECLEARED_API_KEY"] = ""
            _print_harvey("No problem: 10 checks a day without one. Add")
            _print_harvey("PRECLEARED_API_KEY to .env whenever you like.")

    def _write_env(self):
        """Write the .env file, preserving any existing variables we didn't touch."""
        existing = _read_env_file()

        # Wizard answers win, but never blank out a previously-set value
        # (e.g. re-running setup and skipping the LinkedIn step).
        merged = dict(existing)
        for key, value in self.env_vars.items():
            if value or key not in merged:
                merged[key] = value

        lines = [f"{key}={value}" for key, value in merged.items()]
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            ENV_FILE.chmod(0o600)  # credentials — owner read/write only
        except OSError:
            pass
        logger.info(f"Wrote {ENV_FILE}")

    def _write_config(self):
        """Write harvey.yaml (only if we have manual config — trainer writes its own)."""
        from harvey.trainer import brain_section

        config_file = _config_file()
        if self.config:
            self.config["brain"] = brain_section(self.brain_config)
        if self.config and not config_file.exists():
            if self.precleared:
                _save_precleared(self.config, self.precleared)
            with open(config_file, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)
            logger.info(f"Wrote {config_file}")
            self.config_saved = True
        elif self.config:
            # Config already exists (from trainer), update behavior settings only
            try:
                existing = yaml.safe_load(read_user_text(config_file)) or {}
                existing["usage"] = self.config.get("usage", existing.get("usage", {}))
                existing["brain"] = self.config["brain"]
                if "LINKEDIN_EMAIL" in self.env_vars:
                    existing.setdefault("channels", {}).setdefault("linkedin", {})["enabled"] = bool(
                        self.env_vars["LINKEDIN_EMAIL"]
                    )
                if "channels" in self.config:
                    existing.setdefault("channels", {})
                    if "email" in self.config["channels"]:
                        existing["channels"].setdefault("email", {})
                        existing["channels"]["email"]["max_daily_sends"] = (
                            self.config["channels"]["email"].get("max_daily_sends", 50)
                        )
                if self.precleared:
                    _save_precleared(existing, self.precleared)
                text = yaml.dump(existing, default_flow_style=False, sort_keys=False)
                with open(config_file, "w", encoding="utf-8") as f:
                    f.write(text)
                self.config_saved = True
            except Exception as e:
                _print_harvey(f"Couldn't save your settings to {config_file.name} ({e}).")
                _print_harvey("Nothing in it was changed. Fix the file and re-run setup.")

    async def _print_summary(self):
        """Print final summary and next steps."""
        print(f"""
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   Setup Complete!                                        ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝
""")

        # Show what's configured
        checks = {
            ("Codex CLI (ChatGPT plan)" if self.brain_config.provider == "codex"
             else "Claude Code CLI"): (_check_codex_cli() if self.brain_config.provider == "codex"
                                      else _check_claude_cli()),
            "Instantly API": bool(self.env_vars.get("INSTANTLY_API_KEY")),
            "LinkedIn": bool(self.env_vars.get("LINKEDIN_EMAIL")),
            "Cloudflare Crawl": bool(self.env_vars.get("CLOUDFLARE_ACCOUNT_ID")),
            "Product trained": _config_file().exists(),
            "Cold-email check (Precleared)": bool(
                self.config_saved and self.precleared and self.precleared.get("enabled")),
        }

        for name, ok in checks.items():
            status = "✓" if ok else "–"
            print(f"  {status} {name}")

        print(f"""
  Files created:
    - .env (your credentials)
    - {_config_file().name} (your configuration)

  To start Harvey:
    harvey run          (or: python -m harvey)

  To re-train on a different product:
    harvey train https://newproduct.com

  To re-run this setup:
    harvey setup

  Before you send a single email:
    1. Use a dedicated sending domain (not your main domain) with
       SPF, DKIM, and DMARC configured.
    2. Warm it up 2-4 weeks before real volume (Instantly has warmup).
    3. Make sure your Instantly campaigns include an unsubscribe
       option and your physical mailing address (CAN-SPAM).
    See the "Legal & Deliverability" section of the README.
""")
        _print_harvey("I'm ready. Run 'harvey run' and I'll start closing.")
        _print_harvey("Always be closing.\n")


async def run_setup():
    wizard = SetupWizard()
    await wizard.run()


def main():
    asyncio.run(run_setup())


if __name__ == "__main__":
    from harvey.textio import utf8_stdio
    utf8_stdio()
    main()
