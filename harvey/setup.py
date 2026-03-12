"""Interactive setup wizard — Claude walks you through everything Harvey needs."""

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import yaml
from dotenv import load_dotenv

from harvey.brain import Brain
from harvey.state import StateManager

logger = logging.getLogger("harvey.setup")

PROJECT_ROOT = Path(__file__).parent.parent
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
    """Ask the user a question."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"\n  → {prompt}{suffix}: ").strip()
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


def _check_claude_cli() -> bool:
    """Check if Claude CLI is installed and accessible."""
    return shutil.which("claude") is not None


async def _test_claude(brain: Brain) -> bool:
    """Test that Claude Code headless mode works."""
    response = await brain.think(
        "Respond with exactly: HARVEY_READY",
        session_id="harvey-setup-test",
    )
    return "HARVEY_READY" in response


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

        total_steps = 6

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

        # Write everything
        self._write_env()
        self._write_config()

        # Final summary
        await self._print_summary()

    async def _check_prerequisites(self):
        """Verify Claude CLI is installed and working."""
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
        brain = Brain(state)
        if await _test_claude(brain):
            print("✓ Working")
        else:
            print("✗ Failed")
            _print_harvey("Claude CLI is installed but headless mode isn't working.")
            _print_harvey("Make sure you're logged in: run 'claude login'")
            print("\n         Also verify your Max subscription is active.")
            if not _ask_yes_no("Continue anyway?", default=False):
                raise SystemExit(1)

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
            api_key = _ask("Instantly API key (from Settings → Integrations)")
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
        _print_harvey("I'll log into your LinkedIn account using a browser and")
        _print_harvey("search like a human would — with random delays and everything.")

        use_linkedin = _ask_yes_no("Set up LinkedIn prospecting?")

        if use_linkedin:
            email = _ask("LinkedIn email/username")
            password = _ask("LinkedIn password")
            self.env_vars["LINKEDIN_EMAIL"] = email
            self.env_vars["LINKEDIN_PASSWORD"] = password

            _print_harvey("Got it. I'll be careful with your account — human-like")
            _print_harvey("behavior, rate limits, the works. No one will know.")
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
            api_token = _ask("Cloudflare API Token (needs Browser Rendering - Edit permission)")
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
            trainer = Trainer()
            self.config = await trainer.train(url, str(CONFIG_FILE)) or {}

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
        geography = _ask("Target geography", default="United States").split(",")
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
            usage_pct = float(usage_pct)
        except ValueError:
            usage_pct = 80.0

        # Heartbeat interval
        print("\n  I wake up on a schedule to check for work.")
        interval = _ask(
            "How often should I check for work (minutes)?",
            default="15",
        )
        try:
            interval = int(interval)
        except ValueError:
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
            daily_sends = int(daily_sends)
        except ValueError:
            daily_sends = 50

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

    def _write_env(self):
        """Write the .env file."""
        lines = []
        for key, value in self.env_vars.items():
            lines.append(f"{key}={value}")

        ENV_FILE.write_text("\n".join(lines) + "\n")
        logger.info(f"Wrote {ENV_FILE}")

    def _write_config(self):
        """Write harvey.yaml (only if we have manual config — trainer writes its own)."""
        if self.config and not CONFIG_FILE.exists():
            with open(CONFIG_FILE, "w") as f:
                yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)
            logger.info(f"Wrote {CONFIG_FILE}")
        elif self.config:
            # Config already exists (from trainer), update behavior settings only
            try:
                with open(CONFIG_FILE) as f:
                    existing = yaml.safe_load(f) or {}
                existing["usage"] = self.config.get("usage", existing.get("usage", {}))
                if "channels" in self.config:
                    existing.setdefault("channels", {})
                    if "email" in self.config["channels"]:
                        existing["channels"].setdefault("email", {})
                        existing["channels"]["email"]["max_daily_sends"] = (
                            self.config["channels"]["email"].get("max_daily_sends", 50)
                        )
                with open(CONFIG_FILE, "w") as f:
                    yaml.dump(existing, f, default_flow_style=False, sort_keys=False)
            except Exception:
                pass

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
            "Claude Code CLI": _check_claude_cli(),
            "Instantly API": bool(self.env_vars.get("INSTANTLY_API_KEY")),
            "LinkedIn": bool(self.env_vars.get("LINKEDIN_EMAIL")),
            "Cloudflare Crawl": bool(self.env_vars.get("CLOUDFLARE_ACCOUNT_ID")),
            "Product trained": CONFIG_FILE.exists(),
        }

        for name, ok in checks.items():
            status = "✓" if ok else "–"
            print(f"  {status} {name}")

        print(f"""
  Files created:
    - .env (your credentials)
    - harvey.yaml (your configuration)

  To start Harvey:
    python -m harvey

  To re-train on a different product:
    python -m harvey.trainer https://newproduct.com

  To re-run this setup:
    python -m harvey.setup
""")
        _print_harvey("I'm ready. Run 'python -m harvey' and I'll start closing.")
        _print_harvey("Always be closing.\n")


async def run_setup():
    wizard = SetupWizard()
    await wizard.run()


def main():
    asyncio.run(run_setup())


if __name__ == "__main__":
    main()
