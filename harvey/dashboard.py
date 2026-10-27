"""Harvey Dashboard — local web UI to set up, control, and monitor Harvey."""

import asyncio
import ipaddress
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, Response,
)

logger = logging.getLogger("harvey.dashboard")

from harvey import dashboard_auth as auth  # noqa: E402
from harvey.paths import PROJECT_ROOT  # noqa: E402
from harvey.pidfile import LOCK_FILE, loop_lock_held, pid_alive  # noqa: E402
from harvey.textio import read_user_text  # noqa: E402
DB_PATH = PROJECT_ROOT / "data" / "harvey.db"
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "harvey.yaml"
PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"
LOG_FILE = PROJECT_ROOT / "data" / "harvey.log"
TOKEN_FILE = PROJECT_ROOT / "data" / "dashboard-token"


def _resolve_config_file() -> Path:
    """harvey.local.yaml wins when present — same resolution as config.py's
    loader, so the dashboard doesn't judge setup complete against the
    tracked harvey.yaml template while the real trained config sits in the
    gitignored local override."""
    from harvey.config import _find_config_file, ConfigFileNotFoundError
    try:
        return Path(_find_config_file())
    except ConfigFileNotFoundError:
        return CONFIG_FILE

# No /docs, /redoc or /openapi.json: the API is for this UI only, and every
# route except the static shell needs the token (see dashboard_auth).
app = FastAPI(title="Harvey Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
token_store = auth.TokenStore(TOKEN_FILE)
app.add_middleware(auth.DashboardGuard, store=token_store)

# Harvey process tracking
_harvey_process: subprocess.Popen | None = None
_harvey_started_at: datetime | None = None
_env_lock = asyncio.Lock()


# ── Helpers ──


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts.

    Never raises: a missing DB file, missing table, or malformed schema
    returns [] so no dashboard route can 500 on an empty install.
    """
    if not DB_PATH.exists():
        return []
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_db failed (%s): %s", sql.split(None, 4)[:4], e)
        return []


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****" if key else ""
    return key[:4] + "****" + key[-4:]


def _read_env_file() -> dict[str, str]:
    """Read .env file and return as dict."""
    env_vars = {}
    if ENV_FILE.exists():
        for line in read_user_text(ENV_FILE).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def _env_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    key = stripped.partition("=")[0].strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    return key


def _write_env_file(updates: dict[str, str]):
    """Set keys in .env, keeping every other line (comments, order, other
    keys) as it was.

    Written to a private temporary file in the same directory and swapped
    in atomically, so a crash never leaves a half-written .env and the
    credentials are never readable by other users, even for a moment: the
    result is owner-only (0600 on POSIX) whether .env existed or not. A
    symlinked .env is refused rather than replaced or followed.
    """
    if ENV_FILE.is_symlink():
        raise OSError(f"{ENV_FILE} is a symbolic link; edit it by hand.")
    if ENV_FILE.exists() and not ENV_FILE.is_file():
        raise OSError(f"{ENV_FILE} is not a regular file.")
    lines = (read_user_text(ENV_FILE).splitlines()
             if ENV_FILE.exists() else [])

    out, written = [], set()
    for line in lines:
        key = _env_key(line)
        if key in updates:
            if key in written:
                continue  # a later duplicate would override the new value
            out.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in written]

    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(ENV_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    load_dotenv(str(ENV_FILE), override=True)


def _check_harvey_pid() -> int | None:
    """Check if there's a running Harvey process from a PID file."""
    global _harvey_process, _harvey_started_at
    if _harvey_process and _harvey_process.poll() is None:
        return _harvey_process.pid
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        # pid_alive, never os.kill(pid, 0): on Windows that terminates the loop.
        # And only while a loop holds the lock: after a crash or a reboot the
        # PID can belong to an unrelated program, which Stop must not signal.
        if pid is not None and pid_alive(pid) and loop_lock_held(LOCK_FILE):
            return pid
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return None


# ── Setup Status ──


@app.get("/api/setup-status")
async def get_setup_status():
    """Check what's configured and what still needs setup."""
    checks = []

    # 1. Venv
    checks.append({
        "id": "venv", "label": "Python virtual environment",
        "done": (PROJECT_ROOT / ".venv").is_dir(),
        "required": True,
        "help": "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .",
    })

    # 2. Env file
    env_vars = _read_env_file()
    env_exists = ENV_FILE.exists() and bool(env_vars)
    checks.append({
        "id": "env_file", "label": "Environment file (.env)",
        "done": env_exists,
        "required": True,
        "help": "Go to the Settings tab to enter your API keys.",
    })

    # 3. Email provider configured (matches channels.email.provider)
    provider = _current_provider()

    def _has(*keys):
        return all((env_vars.get(k, "") or os.getenv(k, "")).strip() for k in keys)

    if provider == "gmail":
        provider_done = _has("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET") and \
            (PROJECT_ROOT / "data" / "gmail_token.json").is_file()
        provider_help = ("Set GMAIL_CLIENT_ID/SECRET in Settings, then run "
                         "'harvey gmail auth' in your terminal.")
    elif provider == "microsoft":
        provider_done = _has("M365_CLIENT_ID") and \
            (PROJECT_ROOT / "data" / "m365_token.json").is_file()
        provider_help = ("Set the Microsoft 365 client and tenant IDs in Settings, then run "
                         "'harvey microsoft auth' in your terminal.")
    elif provider == "smtp":
        provider_done = _has("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD")
        provider_help = "Enter your SMTP host, username, and password in Settings."
    else:
        instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
        provider_done = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
        provider_help = "Enter your Instantly API key in Settings."
    checks.append({
        "id": "email_provider", "label": f"Email provider configured ({provider})",
        "done": provider_done,
        "required": True,
        "help": provider_help,
    })

    # 4. Email verification (needed for addresses to be sendable, not 'guess')
    verifier_set = _has("REOON_API_KEY") or _has("ZEROBOUNCE_API_KEY") or _has("HUNTER_API_KEY")
    checks.append({
        "id": "verifier", "label": "Email verification key",
        "done": verifier_set,
        "required": True,
        "help": "Add a Reoon (free 600/mo), ZeroBounce, or Hunter key in Settings — "
                "without one, found emails stay 'guess' and are never sent.",
    })

    # 5. Config valid
    config_valid = False
    config_path = _resolve_config_file()
    if config_path.exists():
        try:
            cfg = yaml.safe_load(read_user_text(config_path))
            company = cfg.get("persona", {}).get("company", "")
            product = cfg.get("product", {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": f"Harvey configured ({config_path.name})",
        "done": config_valid,
        "required": True,
        "help": "Train Harvey on your product. Use the trainer or set up manually through Claude.",
    })

    # 6. Product trained
    product_trained = (PROJECT_ROOT / "skills" / "product_knowledge.md").exists()
    checks.append({
        "id": "product_trained", "label": "Product knowledge trained",
        "done": product_trained,
        "required": True,
        "help": "Run: harvey train https://yourwebsite.com (or set up through Claude).",
    })

    # 7. LinkedIn (optional)
    linkedin_email = env_vars.get("LINKEDIN_EMAIL", "") or os.getenv("LINKEDIN_EMAIL", "")
    linkedin_pass = env_vars.get("LINKEDIN_PASSWORD", "") or os.getenv("LINKEDIN_PASSWORD", "")
    checks.append({
        "id": "linkedin", "label": "LinkedIn credentials",
        "done": bool(linkedin_email) and bool(linkedin_pass),
        "required": False,
        "help": "Optional. Enter your LinkedIn credentials in Settings to enable LinkedIn prospecting.",
    })

    # 8. Cloudflare (optional)
    cf_id = env_vars.get("CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cf_token = env_vars.get("CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")
    checks.append({
        "id": "cloudflare", "label": "Cloudflare deep crawling",
        "done": bool(cf_id) and bool(cf_token),
        "required": False,
        "help": "Optional. For JavaScript-rendered website crawling during training.",
    })

    # 9. Warmup service: TrulyInbox connected, or another service the user named
    from harvey.integrations import trulyinbox as ti
    try:
        state = _state()
        await state.init_db()
        choice = await ti.get_choice(state)
    except Exception:
        choice = ""
    if _has("TRULYINBOX_API_KEY"):
        choice = "connected"
    checks.append({
        "id": "warmup_service",
        "label": {"connected": "Warmup: TrulyInbox connected",
                  "other": "Warmup: your own warmup service"}.get(choice, "Warm up your mailbox"),
        "done": bool(choice),
        # Instantly warms its own accounts, so there it's a suggestion, not a step.
        "required": provider != "instantly",
        "help": ("A new mailbox needs 2-4 weeks of warmup before cold email, and warmup "
                 "keeps it out of spam after that. We recommend TrulyInbox: connect it and "
                 "Harvey shows whether your mailbox is warmed."),
        "link": ti.AFFILIATE_URL,
    })

    required_checks = [c for c in checks if c["required"]]
    completed_required = sum(1 for c in required_checks if c["done"])

    return {
        "checks": checks,
        "completed": completed_required,
        "total_required": len(required_checks),
        "percent": int(completed_required / len(required_checks) * 100) if required_checks else 0,
    }


# ── Settings ──


def _current_provider() -> str:
    """Read channels.email.provider from the active config (best-effort)."""
    try:
        cfg = yaml.safe_load(read_user_text(_resolve_config_file())) or {}
        return ((cfg.get("channels") or {}).get("email") or {}).get("provider", "instantly")
    except Exception:
        return "instantly"


@app.get("/api/settings")
async def get_settings():
    """Get current settings — presence flags only for secrets, never raw values."""
    env_vars = _read_env_file()
    all_keys = [
        "INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
        "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
        "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
        "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
        "M365_CLIENT_ID", "M365_TENANT_ID",
        "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
    ]
    for key in all_keys:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    def is_set(k):
        return bool((env_vars.get(k) or "").strip())

    # Gmail is authorized once harvey gmail auth has stored a token file.
    gmail_token = (PROJECT_ROOT / "data" / "gmail_token.json").is_file()

    return {
        "provider": _current_provider(),
        # Non-secret values echo back so fields repopulate; secrets are
        # presence-only so keys never leave the box.
        "instantly_api_key_set": is_set("INSTANTLY_API_KEY"),
        "linkedin_email": env_vars.get("LINKEDIN_EMAIL", ""),
        "linkedin_password_set": is_set("LINKEDIN_PASSWORD"),
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_set": is_set("CLOUDFLARE_API_TOKEN"),
        "gmail_client_id": env_vars.get("GMAIL_CLIENT_ID", ""),
        "gmail_client_secret_set": is_set("GMAIL_CLIENT_SECRET"),
        "gmail_authorized": gmail_token,
        "smtp_host": env_vars.get("SMTP_HOST", ""),
        "smtp_port": env_vars.get("SMTP_PORT", ""),
        "smtp_username": env_vars.get("SMTP_USERNAME", ""),
        "smtp_password_set": is_set("SMTP_PASSWORD"),
        "imap_host": env_vars.get("IMAP_HOST", ""),
        "imap_port": env_vars.get("IMAP_PORT", ""),
        "imap_username": env_vars.get("IMAP_USERNAME", ""),
        "imap_password_set": is_set("IMAP_PASSWORD"),
        # Microsoft 365: ids aren't secret (no client secret: Harvey signs
        # in as a public client with PKCE); the token file means signed in.
        "m365_client_id": env_vars.get("M365_CLIENT_ID", ""),
        "m365_tenant_id": env_vars.get("M365_TENANT_ID", ""),
        "m365_authorized": (PROJECT_ROOT / "data" / "m365_token.json").is_file(),
        "reoon_api_key_set": is_set("REOON_API_KEY"),
        "zerobounce_api_key_set": is_set("ZEROBOUNCE_API_KEY"),
        "hunter_api_key_set": is_set("HUNTER_API_KEY"),
    }


@app.post("/api/settings/env")
async def save_env_settings(request: Request):
    """Save environment variables to .env file."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        updates = {}
        for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                     "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
                     "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                     "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                     "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
                     "M365_CLIENT_ID", "M365_TENANT_ID",
                     "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY"]:
            if key in data and data[key] is not None:
                value = data[key]
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    return JSONResponse({"success": False, "message": f"{key} must be text."},
                                        status_code=400)
                value = str(value)
                # A port Harvey can't parse would stop it starting at all.
                if key.endswith("_PORT") and value.strip() and not value.strip().isdigit():
                    return JSONResponse({"success": False, "message": f"{key} must be a number."},
                                        status_code=400)
                if len(value) > 4096 or "\x00" in value:
                    return JSONResponse({"success": False, "message": f"{key} is invalid."},
                                        status_code=400)
                # Strip newlines so a crafted value can't inject extra .env entries
                updates[key] = value.replace("\n", " ").replace("\r", " ").strip()
        if updates:
            try:
                _write_env_file(updates)
            except Exception as e:
                logger.warning("Failed to write .env: %s", e)
                return {"success": False, "message": "Could not write .env file."}
    return {"success": True}


@app.post("/api/trulyinbox/connect")
async def trulyinbox_connect(request: Request):
    """Check a TrulyInbox key against the sending mailbox, then save it to .env."""
    from harvey import deliverability as d
    from harvey.integrations import trulyinbox as ti
    try:
        data = await request.json()
    except Exception:
        data = None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip() or len(key) > 512:
        return JSONResponse({"success": False, "message": "Paste your TrulyInbox API key."},
                            status_code=400)
    key = key.strip()
    try:
        from harvey.config import load_config
        config = load_config()
        address, warmup = ti.sending_address(config), config.channels.email.warmup
    except Exception:
        address, warmup = "", None
    if not address:
        return JSONResponse({"success": False, "message": ti.NO_ADDRESS}, status_code=400)
    workspace = (_read_env_file().get("TRULYINBOX_WORKSPACE_ID", "")
                 or os.getenv("TRULYINBOX_WORKSPACE_ID", "")).strip()
    ti.forget_rejected(key)
    try:
        client = ti.TrulyInbox(key, workspace)
        health = await client.mailbox_health(address)
    except ti.TrulyInboxError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    updates = {"TRULYINBOX_API_KEY": key}
    if not workspace:
        updates["TRULYINBOX_WORKSPACE_ID"] = client.workspace_id
    async with _env_lock:
        try:
            _write_env_file(updates)
        except Exception as e:
            logger.warning("Failed to write .env: %s", e)
            return {"success": False, "message": "Could not write .env file."}
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "connected")
    return {"success": True, "message": d.check_warmup_service(health, warmup).detail}


@app.post("/api/trulyinbox/other")
async def trulyinbox_other():
    """The user warms their mailbox with another service: stop recommending one."""
    from harvey.integrations import trulyinbox as ti
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "other")
    return {"success": True}


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    api_key = str(data.get("api_key", "") or "")
    if not api_key:
        return {"success": False, "message": "No API key provided."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return {"success": True, "message": "Connected to Instantly."}
            else:
                return {"success": False, "message": f"API returned {resp.status_code}. Check your key."}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


# ── Companies ──


@app.get("/api/companies")
async def get_companies():
    """All companies with contact counts."""
    rows = await query_db("""
        SELECT c.*,
            (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) as contact_count
        FROM companies c ORDER BY c.created_at DESC LIMIT 200
    """)
    return rows


@app.get("/api/companies/{company_id}/contacts")
async def get_company_contacts(company_id: str):
    """Get all contacts for a specific company."""
    rows = await query_db(
        "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
        (company_id,),
    )
    return rows


# ── Feedback ──


@app.post("/api/feedback")
async def add_feedback(request: Request):
    """Add a comment/feedback on any entity."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    entity_type = str(data.get("entity_type", "") or "")[:50]
    entity_id = str(data.get("entity_id", "") or "")[:100]
    comment = str(data.get("comment", "") or "").strip()[:4000]
    if not comment:
        return {"success": False, "message": "Comment is required."}
    feedback_id = uuid.uuid4().hex[:12]
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            # Ensure the table exists so feedback works even on a fresh install
            await db.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT,
                    entity_id TEXT,
                    comment TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )"""
            )
            await db.execute(
                "INSERT INTO feedback (id, entity_type, entity_id, comment) VALUES (?, ?, ?, ?)",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save feedback: %s", e)
        return {"success": False, "message": "Could not save feedback."}
    return {"success": True, "id": feedback_id}


@app.get("/api/feedback/{entity_type}/{entity_id}")
async def get_feedback(entity_type: str, entity_id: str):
    """Get feedback for an entity."""
    rows = await query_db(
        "SELECT * FROM feedback WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
        (entity_type, entity_id),
    )
    return rows


# ── Harvey Controls ──


@app.get("/api/harvey/status")
async def get_harvey_status():
    """Check if Harvey is currently running."""
    pid = _check_harvey_pid()
    started = _harvey_started_at.isoformat() if _harvey_started_at else None
    return {"running": pid is not None, "pid": pid, "started_at": started}


@app.post("/api/harvey/start")
async def start_harvey():
    """Start Harvey's heartbeat loop as a subprocess."""
    global _harvey_process, _harvey_started_at

    if _check_harvey_pid():
        return {"success": False, "message": "Harvey is already running."}
    from harvey.main import _needs_setup
    if _needs_setup():
        return {"success": False,
                "message": "Harvey isn't set up yet. Run `harvey setup` in a terminal first."}

    # Ensure data dir exists
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

    # The native Claude CLI installer puts claude in ~/.local/bin without
    # always adding it to PATH; the heartbeat can't think without it.
    env = os.environ.copy()
    local_bin = Path.home() / ".local" / "bin"
    if not shutil.which("claude") and local_bin.is_dir():
        env["PATH"] = str(local_bin) + os.pathsep + env.get("PATH", "")

    try:
        log_handle = open(LOG_FILE, "a", encoding="utf-8")
        try:
            _harvey_process = subprocess.Popen(
                [sys.executable, "-m", "harvey", "run"],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                env=env,
            )
        finally:
            # Child holds its own copies of the fds; don't leak ours.
            log_handle.close()
    except Exception as e:
        logger.warning("Failed to start Harvey: %s", e)
        return {"success": False, "message": f"Failed to start Harvey: {e}"}
    _harvey_started_at = datetime.now()
    # The loop writes its own PID file once it holds the loop lock. Writing
    # the child's PID here would overwrite a running loop's record if this
    # child then finds the lock taken and exits.
    return {"success": True, "pid": _harvey_process.pid}


@app.post("/api/harvey/stop")
async def stop_harvey():
    """Stop the Harvey subprocess."""
    global _harvey_process, _harvey_started_at

    pid = _check_harvey_pid()
    if not pid:
        return {"success": False, "message": "Harvey is not running."}

    own = _harvey_process if _harvey_process and _harvey_process.pid == pid else None

    def alive() -> bool:
        # Our own child is reaped by poll(); left unreaped it is a zombie,
        # which still answers signal 0.
        if own is not None:
            return own.poll() is None
        return pid_alive(pid)

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            if not alive():
                break
            await asyncio.sleep(0.5)
        else:
            # Force kill if still running (Windows has no SIGKILL; SIGTERM
            # there is already TerminateProcess)
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
            await asyncio.sleep(0.5)
    except OSError as e:
        logger.warning("Couldn't signal Harvey (pid %s): %s", pid, e)

    if alive():
        # Started by another user, or from an elevated terminal on Windows.
        return {"success": False,
                "message": f"Couldn't stop Harvey (pid {pid}). Stop it where it was started."}

    _harvey_process = None
    _harvey_started_at = None
    PID_FILE.unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/harvey/logs")
async def get_harvey_logs():
    """Get recent log lines."""
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        # Tail only the last 64KB so a huge log file never blocks the UI
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.strip().splitlines()[-100:]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


# ── Pipeline Data (existing endpoints) ──


def _pipeline_bucket(status: str | None, stage: str | None, intent: str | None,
                     email_status: str | None = None) -> str:
    """One funnel stage per contact, from its status and latest conversation."""
    status = status or "new"
    if status in ("new", "queued"):
        return "to_write"
    if status == "contacted":
        # A bounce leaves the status alone and marks the address invalid.
        return "closed_out" if email_status == "invalid" else "contacted"
    if status == "meeting":
        return "ready_to_meet"
    if status == "replied" and stage != "closed_lost":
        if stage == "closing":
            return "ready_to_meet"
        if intent == "interested" or stage in ("presenting", "negotiating"):
            return "interested"
        return "replied"
    return "closed_out"   # lost, opted out, bounced, unwritable


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        prospects = await query_db(
            "SELECT status, COUNT(*) as count FROM prospects GROUP BY status"
        )
        prospect_total = sum(r["count"] for r in prospects)
        prospect_map = {r["status"]: r["count"] for r in prospects}

        campaigns = await query_db(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status"
        )
        campaign_map = {r["status"]: r["count"] for r in campaigns}

        conversations = await query_db(
            "SELECT status, COUNT(*) as count FROM conversations GROUP BY status"
        )
        convo_map = {r["status"]: r["count"] for r in conversations}

        # Where each contact stands. Prospect status alone stops at
        # "replied"; how a reply went lives on its latest conversation.
        rows = await query_db("""
            SELECT p.status, p.email_status, l.stage, l.intent, COUNT(*) AS count FROM prospects p
            LEFT JOIN (SELECT prospect_id, stage, intent, ROW_NUMBER() OVER (
                         PARTITION BY prospect_id ORDER BY updated_at DESC, created_at DESC) AS rn
                       FROM conversations) l ON l.prospect_id = p.id AND l.rn = 1
            GROUP BY p.status, p.email_status, l.stage, l.intent
        """)
        pipeline = dict.fromkeys(
            ("to_write", "contacted", "replied", "interested", "ready_to_meet", "closed_out"), 0)
        for r in rows:
            pipeline[_pipeline_bucket(r["status"], r["stage"], r["intent"],
                                      r["email_status"])] += r["count"]

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        # usage_log rows are keyed by the LOCAL date (increment_usage /
        # get_usage_today use date.today()); SQLite's date('now') is UTC and
        # can select a different day's counts on either side of UTC midnight.
        usage = await query_db(
            "SELECT claude_calls, codex_calls FROM usage_log WHERE date = ?",
            (date.today().isoformat(),),
        )
        usage_today = (usage[0]["claude_calls"] or 0) if usage else 0
        codex_today = (usage[0]["codex_calls"] or 0) if usage else 0

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "pipeline": pipeline,
            "actions_total": action_count,
            "claude_calls_today": usage_today,
            "codex_calls_today": codex_today,
            "brain_provider": _active_provider(),
            "brain_status": await _brain_status(),
        }
    except Exception as e:
        return {"error": str(e)}


_USAGE_SUM = (
    "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
    "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
    "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
    "ROUND(COALESCE(SUM(cost_usd), 0), 4) AS cost_usd"
)

_quota_clients: dict = {}


def _active_brain():
    """The brain config harvey.yaml selects (None if unreadable)."""
    try:
        from harvey.config import load_brain_config
        return load_brain_config()
    except Exception:
        return None


async def _brain_status() -> dict:
    """Last recorded brain health transition ({} if none): lets the
    dashboard say "Codex paused: <why>" instead of looking healthy."""
    from harvey.brain import BRAIN_STATUS_KEY, active_brain_status
    rows = await query_db("SELECT value FROM settings WHERE key = ?", (BRAIN_STATUS_KEY,))
    # Only the configured provider's health (a stale Codex pause isn't
    # Claude's).
    return active_brain_status(rows[0]["value"], _active_provider()) if rows else {}


def _active_provider() -> str:
    brain = _active_brain()
    return brain.provider if brain else ""


def _quota_client_for(provider: str, model: str = ""):
    """One cached quota reader per provider (and Codex model). Each reads
    only its own subscription, so a Claude gauge is never shown as Codex or
    vice versa, and Codex shows the bucket metering the configured model."""
    key = (provider, model if provider == "codex" else "")
    if key not in _quota_clients:
        if provider == "codex":
            from harvey.codex import CodexClient, CodexQuotaClient
            _quota_clients[key] = CodexQuotaClient(CodexClient(model=model))
        elif provider == "claude":
            from harvey.integrations.quota import QuotaClient
            _quota_clients[key] = QuotaClient()
        else:
            return None
    return _quota_clients[key]


@app.get("/api/usage")
async def get_usage():
    """Token/cost accounting + live subscription quota for the Usage tab."""

    totals = {}
    for label, where in (
        ("today", "date(created_at) = date('now')"),
        ("week", "created_at >= datetime('now', '-7 days')"),
        ("month", "created_at >= datetime('now', '-30 days')"),
    ):
        rows = await query_db(f"SELECT {_USAGE_SUM} FROM usage_events WHERE {where}")
        totals[label] = rows[0] if rows else {}

    def grouped(expr, alias):
        return (
            f"SELECT {expr} AS {alias}, {_USAGE_SUM} FROM usage_events "
            f"WHERE created_at >= datetime('now', '-30 days') "
            f"GROUP BY {alias} ORDER BY output_tokens DESC LIMIT 25"
        )

    by_agent = await query_db(grouped("CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent"))
    by_task = await query_db(grouped("CASE WHEN task = '' THEN 'other' ELSE task END", "task"))
    by_model = await query_db(grouped("CASE WHEN model = '' THEN 'unknown' ELSE model END", "model"))
    by_provider = await query_db(grouped("provider", "provider"))
    by_day = await query_db(
        f"SELECT date(created_at) AS day, {_USAGE_SUM} FROM usage_events "
        f"WHERE created_at >= datetime('now', '-30 days') "
        f"GROUP BY day ORDER BY day ASC"
    )

    brain = _active_brain()
    provider = brain.provider if brain else ""
    quota = None
    try:
        client = _quota_client_for(provider, brain.model if brain else "")
        if client is not None:
            quota = await client.get_utilization()
    except Exception as e:
        logger.debug("Quota lookup failed: %s", e)

    return {
        "provider": provider,
        "quota": quota,
        # Whose quota this is, even when it couldn't be read, so the UI shows
        # "unavailable" for the active provider instead of hiding the gauge.
        "quota_provider": provider,
        "quota_status": ("available" if quota else "unavailable") if provider else "",
        "by_provider": by_provider,
        "totals": totals,
        "by_day": by_day,
        "by_agent": by_agent,
        "by_task": by_task,
        "by_model": by_model,
    }


@app.get("/api/prospects")
async def get_prospects():
    rows = await query_db("SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200")
    # Someone whose opt-out was undone keeps a way to see what happened.
    undone = {r["pid"] for r in await query_db(
        "SELECT DISTINCT json_extract(details_json, '$.prospect_id') AS pid FROM actions "
        "WHERE action_type = 'opt_out_undone' AND json_valid(details_json)")}
    for r in rows:
        r["opt_out_undone"] = r.get("id") in undone
    return rows


def _state():
    from harvey.state import StateManager
    return StateManager(db_path=str(DB_PATH))


# What a reviewer needs next to a draft to check its claims against the facts.
OUTBOX_CONTEXT_SIGNALS = (
    "TECH_STACK", "INCUMBENT_AGENCY", "HIRING_ROLE", "RUNNING_GOOGLE_ADS", "NO_ONLINE_BOOKING",
)


def _in(ids) -> str:
    return ",".join("?" for _ in ids)


async def _with_context(items: list[dict]) -> list[dict]:
    """Attach the campaign, company, contact and signals behind each draft."""
    if not items:
        return items
    pids = sorted({i["prospect_id"] for i in items if i.get("prospect_id")})
    cids = sorted({i["campaign_id"] for i in items if i.get("campaign_id")})

    people = {r["id"]: r for r in (await query_db(
        "SELECT p.id, p.first_name, p.last_name, p.title, p.source, p.email_status, "
        "p.score, p.personalization_notes, p.company_id, c.name AS company_name, "
        "c.domain, c.website, c.description, c.industry AS found_by "
        "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id "
        f"WHERE p.id IN ({_in(pids)})", tuple(pids)) if pids else [])}
    campaigns = {r["id"]: r["name"] for r in (await query_db(
        f"SELECT id, name FROM campaigns WHERE id IN ({_in(cids)})",
        tuple(cids)) if cids else [])}

    company_ids = sorted({p["company_id"] for p in people.values() if p.get("company_id")})
    signals: dict[str, dict[str, str]] = {}
    if company_ids:
        rows = await query_db(
            "SELECT o.company_id, o.signal_code, o.value_num, o.value_text, "
            "o.observed_at, o.run_id, "
            "COALESCE(NULLIF(sc.label, ''), o.signal_code) AS label FROM observations o "
            "LEFT JOIN signal_codes sc ON sc.code = o.signal_code "
            f"WHERE o.company_id IN ({_in(company_ids)}) "
            f"AND o.signal_code IN ({_in(OUTBOX_CONTEXT_SIGNALS)}) "
            "ORDER BY o.observed_at, o.id",
            (*company_ids, *OUTBOX_CONTEXT_SIGNALS),
        )
        # Oldest first, so the latest observation wins. A signal can be seen
        # several times at once (one HIRING_ROLE row per open role): every
        # value from the latest sighting shows, not just one of them.
        latest: dict[tuple, tuple] = {}
        for r in rows:
            if r["value_text"]:
                value = r["value_text"]
            elif r["value_num"] is None:
                value = ""
            else:
                value = "yes" if r["value_num"] else "no"
            key = (r["company_id"], r["label"])
            seen = (r["run_id"] or r["observed_at"])
            prev_seen, values = latest.get(key, (None, []))
            if seen != prev_seen:
                values = []
            if value not in values:
                values = values + [value]
            latest[key] = (seen, values)
        for (company_id, label), (_, values) in latest.items():
            signals.setdefault(company_id, {})[label] = ", ".join(v for v in values if v)

    for item in items:
        p = people.get(item.get("prospect_id")) or {}
        item["context"] = {
            "campaign": campaigns.get(item.get("campaign_id"), ""),
            "company": p.get("company_name", ""),
            "website": p.get("website") or (f"https://{p['domain']}" if p.get("domain") else ""),
            "description": p.get("description", ""),
            "found_by": p.get("found_by", ""),
            "contact": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
            "contact_title": p.get("title", ""),
            "contact_source": p.get("source", ""),
            "email_status": p.get("email_status", ""),
            "score": p.get("score"),
            "notes": p.get("personalization_notes", ""),
            "signals": signals.get(p.get("company_id"), {}),
        }
    return items


@app.get("/api/outbox")
async def get_outbox_api():
    """Outbox queue + kill-switch state for the Outbox tab."""
    try:
        state = _state()
        await state.init_db()
        # A send cut off mid-way shows up under Didn't send even when Harvey
        # isn't running.
        await state.fail_stale_sending_outbox()
        return {
            **_compliance_view(),
            "paused": await state.get_setting("sending_paused"),
            "pending": await _with_context(
                await state.get_outbox(status="pending_review", limit=100)),
            # Mail going out right now leads the list.
            "approved": (await state.get_outbox(status="sending", limit=10)
                         + await state.get_outbox(status="approved", limit=50)),
            "sent": (await query_db(
                "SELECT * FROM outbox WHERE status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 25")),
            "failed": (await query_db(
                "SELECT * FROM outbox WHERE status IN ('failed','rejected','cancelled') "
                "ORDER BY updated_at DESC LIMIT 25")),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sending-mode")
async def get_sending_mode():
    """Approval (every email waits for you) or autopilot, and what that means here."""
    from harvey.config import load_config
    from harvey.gate import _booking_prefix, allowed_link_domains
    path = _resolve_config_file()
    try:
        config = load_config(str(path))
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read {path.name}: {e}"}, status_code=500)
    state = _state()
    await state.init_db()
    return {
        "mode": "approval" if config.channels.email.require_approval else "autopilot",
        "provider": config.channels.email.provider,
        "file": path.name,
        "your_domains": sorted(allowed_link_domains(config)),
        "booking_page": _booking_prefix(config),
        "held": await state.count_held_for_review(),
    }


@app.post("/api/sending-mode")
async def set_sending_mode(request: Request):
    """Switch it. Autopilot needs {"mode": "autopilot", "confirm": "autopilot"};
    {"mode": "approval"} works at once and sends back to the Outbox whatever
    autopilot approved that hasn't gone out."""
    from harvey.config import ConfigError, set_require_approval
    try:
        body = await request.json()
    except ValueError:
        body = None
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("approval", "autopilot"):
        return _refuse('expected {"mode": "approval"} or {"mode": "autopilot", "confirm": "autopilot"}', 400)
    if mode == "autopilot" and body.get("confirm") != "autopilot":
        return _refuse('Type "autopilot" to confirm: Harvey will send email without asking you.', 400)
    path = _resolve_config_file()
    try:
        set_require_approval(mode == "approval", path)
    except ConfigError as e:
        return _refuse(str(e), 409)
    state = _state()
    await state.init_db()
    requeued = await state.requeue_autopilot_approved() if mode == "approval" else 0
    await state.log_action("sending_mode", "dashboard", {"mode": mode, "file": path.name,
                                                         "requeued": requeued})
    out = {"success": True, "mode": mode}
    if mode == "approval":
        out["requeued"] = requeued
    return out


@app.post("/api/outbox/approve-all")
async def outbox_approve_all():
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox()
        # Held ones (possible planted link or leak) are approved one by one.
        return {"success": True, "approved": n, "held": await state.count_held_for_review()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/approve")
async def outbox_approve(item_id: str):
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox(item_id)
        return {"success": bool(n)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/edit")
async def outbox_edit(item_id: str, request: Request):
    """Rewrite a draft awaiting review. Never approves it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    subject = str(body.get("subject") or "").strip()
    text = str(body.get("body") or "").strip()
    try:
        state = _state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item or item.get("status") != "pending_review":
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        if item.get("kind") == "reply":
            # Gmail threads a reply by its subject: a new one would land as a
            # fresh, unthreaded email to someone who wrote in. Only the body
            # of a reply is editable; its stored subject is kept exactly.
            subject = item.get("subject") or ""
        if not subject.strip() or not text:
            return JSONResponse({"success": False,
                                 "message": "Subject and body are both required."},
                                status_code=400)
        # The sender re-checks every email before it goes out; an edit it
        # would block is refused now, while the reviewer can still fix it,
        # instead of failing quietly at send time. Content rules only: the
        # recipient isn't something an edit can change.
        from harvey.gate import pre_send_check
        checked = text
        footer = _compliance_view()["footer"]
        if item.get("kind") == "sequence" and footer and not text.rstrip().endswith(footer):
            checked = text.rstrip() + "\n\n" + footer  # as the Sender will send it
        gate = pre_send_check("reviewer@example.com", subject, checked)
        if not gate:
            return JSONResponse({"success": False,
                                 "message": "The sender would have blocked this: "
                                            + "; ".join(gate.reasons)},
                                status_code=400)
        if not await state.edit_outbox_draft(item_id, subject, text):
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        await state.log_action("outbox_edited", "dashboard", {
            "outbox_id": item_id, "to_email": item.get("to_email", ""),
            "old_subject": item.get("subject", ""), "old_body": item.get("body", ""),
        })
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# A hand send is only offered to someone Harvey could still email: never to
# someone who replied, opted out, bounced or was already contacted.
HAND_SENDABLE_STATUSES = ("new", "queued")
HAND_SENDABLE_EMAIL = ("verified", "risky")


def _harvey_can_send() -> bool:
    """Can Harvey send email itself (a connected mailbox, or Instantly)?

    Sending by hand from the dashboard is only offered when it can't: with
    two senders, any timing gap between them emails someone twice. If the
    config can't be read, the answer is yes (the safe side: no hand sends).
    """
    try:
        from harvey.config import load_config, load_env
        from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
        config, env = load_config(), load_env()
        from harvey.config import legacy_sending_held
        email = config.channels.email
        if not email.enabled or legacy_sending_held(config):
            return False
        if (email.provider or "").strip().lower() in NATIVE_PROVIDERS:
            provider = get_mail_provider(config, env)
            return bool(provider and provider.is_configured())
        return bool(env.instantly_api_key)
    except Exception:
        return True


def _send_as() -> str:
    """The address Harvey sends as, so Gmail opens the right account."""
    try:
        from harvey.config import load_config
        return load_config().persona.email or ""
    except Exception:
        return ""


def _compliance_view() -> dict:
    """What the Sender adds under each cold email, so a reviewer sees the
    email as it will leave, and the warning when the address is missing."""
    try:
        from harvey.config import compliance_footer_lines, compliance_notice, load_config
        config = load_config()
        return {"footer": "\n".join(compliance_footer_lines(config)),
                "compliance_notice": compliance_notice(config)}
    except Exception:
        return {"footer": "", "compliance_notice": ""}


async def _harvey_committed_ids() -> set[str]:
    """Everyone Harvey has approved or sent mail for (Outbox or Instantly).

    Approved mail can be going out at this moment, so these people are past
    the point where a hand send can be recorded safely. So is anyone whose
    send was cut off mid-way: it may have reached them. A draft that only
    awaits review isn't: marking the person sent cancels it.
    """
    from harvey.state import MAYBE_SENT_PREFIX
    ids = {r["prospect_id"] for r in await query_db(
        "SELECT DISTINCT prospect_id FROM outbox WHERE status IN ('approved', 'sending', 'sent') "
        "OR (status = 'failed' AND substr(error, 1, ?) = ?)",
        (len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX))}
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            found = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(found, list):
            ids.update(i for i in found if isinstance(i, str))
    return ids


async def _hand_sent_ids() -> set[str]:
    from harvey.state import MANUAL_SEND_KEY
    rows = await query_db("SELECT key FROM settings WHERE key LIKE ?", (MANUAL_SEND_KEY + "%",))
    return {r["key"][len(MANUAL_SEND_KEY):] for r in rows}


async def _harvey_sent_to(prospect_id: str) -> bool:
    """Did Harvey itself email this person (native send, or an Instantly campaign)?

    A send in flight, or cut off mid-way, counts: it may have reached them.
    """
    from harvey.state import MAYBE_SENT_PREFIX
    if await query_db(
            "SELECT 1 FROM outbox WHERE prospect_id = ? AND (status IN ('sending', 'sent') "
            "OR (status = 'failed' AND substr(error, 1, ?) = ?)) LIMIT 1",
            (prospect_id, len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX)):
        return True
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            if prospect_id in json.loads(row.get("prospect_ids_json") or "[]"):
                return True
        except (json.JSONDecodeError, TypeError):
            continue
    return False


def _refuse(message: str, code: int = 409):
    return JSONResponse({"success": False, "message": message}, status_code=code)


@app.post("/api/prospects/{prospect_id}/sent")
async def mark_prospect_sent(prospect_id: str, request: Request):
    """The user sent this email by hand (e.g. from Gmail): record it, or undo it."""
    from harvey.state import HAND_SENT_REASON, MANUAL_SEND_KEY
    try:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("sent", True), bool):
            return _refuse('expected {"sent": true|false}', 400)
        sent = body.get("sent", True)
        rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
        if not rows:
            return _refuse("unknown prospect", 404)
        current = rows[0].get("status") or "new"
        state = _state()
        await state.init_db()
        key = MANUAL_SEND_KEY + prospect_id
        marked = bool(await state.get_setting(key))

        if sent:
            if marked:
                return {"success": True, "sent": True, "already": True}
            if current not in HAND_SENDABLE_STATUSES:
                # A reply, opt-out or close is never touched; neither is
                # someone Harvey already emailed.
                return {"success": True, "sent": True, "status": current}
            # Allowed even when Harvey can send: recording "I already emailed
            # them" can only ever stop Harvey sending. Not once Harvey has
            # approved or sent mail to them, though: that may be going out now.
            # The marker goes down FIRST: a drain that checks after this sees
            # it and cancels; one that checked before only holds an approved
            # row, which the check below then sees.
            await state.set_setting(key, current)
            if prospect_id in await _harvey_committed_ids():
                await state.delete_setting(key)
                return _refuse("Harvey has already approved or sent an email to this person. "
                               "Check the Outbox.")
            await state.update_prospect_status(prospect_id, "contacted")
            # Anything Harvey had queued for them must not go out as well.
            changed = await state.cancel_pending_outbox_for_prospect(
                prospect_id, reason=HAND_SENT_REASON)
        else:
            if not marked:
                if current in HAND_SENDABLE_STATUSES:
                    return {"success": True, "sent": False}
                if current == "contacted":
                    # Setting them back would make Harvey write and send again.
                    return _refuse("This wasn't marked as sent by hand here, so it stays sent.")
                return {"success": True, "sent": True, "status": current}
            if current != "contacted":
                # They replied, opted out or bounced since: that stands.
                return {"success": True, "sent": True, "status": current}
            previous = await state.get_setting(key) or "queued"
            await state.update_prospect_status(
                prospect_id, previous if previous in HAND_SENDABLE_STATUSES else "queued")
            await state.delete_setting(key)
            # What marking sent cancelled comes back for review, never straight out.
            changed = await state.restore_hand_cancelled_outbox(
                prospect_id, reason=HAND_SENT_REASON)
        if sent:
            # Open in Gmail adds today's footer; remember it as Harvey's text.
            footer = _compliance_view()["footer"]
            await state.remember_footer_lines(footer.split("\n"))
        await state.log_action(
            action_type="manual_send" if sent else "manual_send_undone",
            agent="user",
            details={"prospect_id": prospect_id, "outbox_items": changed},
        )
        return {"success": True, "sent": sent}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/reject")
async def outbox_reject(item_id: str):
    try:
        state = _state()
        await state.init_db()
        if not await state.reject_outbox_item(item_id):
            return _refuse("That email is already sending or sent, so it can't be rejected.")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/sending/{action}")
async def sending_toggle(action: str):
    if action not in ("pause", "resume"):
        return JSONResponse({"success": False, "message": "unknown action"}, status_code=400)
    try:
        state = _state()
        await state.init_db()
        if action == "pause":
            await state.set_setting("sending_paused", "paused from dashboard")
        else:
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/export/prospects.csv")
async def export_prospects(all: bool = False, min_score: int = 0, email_status: str = ""):
    """Sequencer-ready CSV download of the prospect list."""
    from harvey.state import StateManager
    from harvey.export import export_prospects_csv

    state = StateManager(db_path=str(DB_PATH))
    try:
        await state.init_db()
        statuses = [s.strip() for s in email_status.split(",") if s.strip()] or None
        _, text = await export_prospects_csv(
            state, email_statuses=statuses, min_score=min_score, include_all=all,
        )
    except Exception as e:
        logger.warning("Prospect export failed: %s", e)
        text = ""
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="prospects.csv"'},
    )


@app.get("/api/campaigns")
async def get_campaigns():
    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100")
    harvey_sends = _harvey_can_send()
    send_as = "" if harvey_sends else _send_as()
    footer = _compliance_view()["footer"]
    committed = await _harvey_committed_ids()
    hand_sent = await _hand_sent_ids()
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
        # Who each draft goes to, so the UI can open it in the user's own mail client.
        ids = row["prospect_ids"]
        row["recipients"] = await query_db(
            f"SELECT id, email, first_name, last_name, company, title, status, email_status, "
            f"source, personalization_notes FROM prospects "
            f"WHERE id IN ({','.join('?' * len(ids))}) AND email != ''",
            tuple(ids),
        ) if ids else []
        row["send_as"] = send_as
        # A cold email sent by hand needs the same footer Harvey adds.
        row["footer"] = footer
        for r in row["recipients"]:
            r["sent_by_hand"] = r["id"] in hand_sent
            # "I already emailed them": always recordable before Harvey commits.
            r["can_mark_sent"] = (
                r["id"] not in hand_sent and r["id"] not in committed
                and (r.get("status") or "new") in HAND_SENDABLE_STATUSES
            )
            # The Gmail button: only when Harvey can't send itself.
            r["can_send_by_hand"] = (
                r["can_mark_sent"] and not harvey_sends
                and (r.get("email_status") or "guess") in HAND_SENDABLE_EMAIL
            )
    return rows


@app.get("/api/prospects/{prospect_id}/opt-out")
async def get_opt_out(prospect_id: str):
    """Why this person is opted out: the rule, the words and their message."""
    rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return _refuse("unknown prospect", 404)
    status = rows[0].get("status") or "new"
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), None)
    out = {
        "opted_out": status == "opted_out", "status": status, "recorded": bool(last),
        "at": None, "rule": None, "matched": "", "why": "", "message": "",
        "conversation_id": "", "history": history,
    }
    if last:
        out.update({k: last.get(k) or out[k] for k in
                    ("rule", "matched", "why", "message", "conversation_id")})
        out["at"] = last["at"]
    elif status == "opted_out" or history:
        # Opted out before Harvey kept reasons (and maybe undone since):
        # show the last thing they wrote.
        for c in await query_db(
                "SELECT id, thread_json, updated_at FROM conversations "
                "WHERE prospect_id = ? ORDER BY updated_at DESC", (prospect_id,)):
            try:
                thread = json.loads(c.get("thread_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            theirs = [m for m in thread if isinstance(m, dict) and m.get("sender") == "prospect"]
            if theirs:
                out.update(message=str(theirs[-1].get("content") or ""),
                           conversation_id=c["id"], at=theirs[-1].get("timestamp") or c.get("updated_at"))
                break
    return out


@app.post("/api/prospects/{prospect_id}/opt-out/undo")
async def undo_opt_out(prospect_id: str, request: Request):
    """The owner says this opt-out was a mistake. Needs {"confirm": true}."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _refuse('expected {"confirm": true}', 400)
    rows = await query_db("SELECT email FROM prospects WHERE id = ?", (prospect_id,))
    if not rows:
        return _refuse("unknown prospect", 404)
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), {})
    if not await state.undo_opt_out(prospect_id, details={
            "prospect_email": rows[0].get("email") or "",
            "previous_why": last.get("why", ""), "previous_at": last.get("at", "")}):
        return _refuse("This person isn't opted out.")
    return {"success": True, "status": "replied"}


@app.get("/api/conversations")
async def get_conversations(awaiting: bool = False):
    rows = await query_db("""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company,
               p.status as prospect_status
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    # The stored thread holds replies and Harvey's answers; the cold emails
    # a reply answers live in the Outbox. Attach those (sent ones only) and
    # any answer still waiting for approval, so the page shows the exchange.
    pids = sorted({r["prospect_id"] for r in rows if r.get("prospect_id")})
    emails: dict[str, list] = {}
    if pids:
        for e in await query_db(
                "SELECT prospect_id, campaign_id, step, subject, body, sent_at FROM outbox "
                f"WHERE kind = 'sequence' AND status = 'sent' AND prospect_id IN ({_in(pids)}) "
                "ORDER BY sent_at", tuple(pids)):
            emails.setdefault(e.pop("prospect_id"), []).append(e)
    cids = [r["id"] for r in rows]
    drafts = {d.pop("conversation_id"): d for d in (await query_db(
        "SELECT id, conversation_id, subject, body, send_at FROM outbox "
        f"WHERE kind = 'reply' AND status = 'pending_review' AND conversation_id IN ({_in(cids)}) "
        "ORDER BY created_at", tuple(cids)) if cids else [])}
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
        # A prospect in two campaigns: show the sequence this reply answers.
        camp = row.get("campaign_id") or ""
        row["emails"] = [{k: v for k, v in e.items() if k != "campaign_id"}
                         for e in emails.get(row.get("prospect_id"), [])
                         if not camp or e.get("campaign_id") == camp]
        row["draft"] = drafts.get(row["id"])
    if awaiting:
        rows += await _awaiting_reply({r.get("prospect_id") for r in rows})
    return rows


async def _awaiting_reply(have: set) -> list[dict]:
    """Prospects Harvey emailed who haven't replied yet, shaped like a
    conversation so the tab can show every sent email, not only the ones
    that got an answer. Read-only: no conversation row is created, and the
    card moves to the real conversation the moment a reply arrives."""
    waiting: dict[str, dict] = {}
    for e in await query_db(
            "SELECT o.prospect_id, o.step, o.subject, o.body, o.sent_at, "
            "p.first_name, p.last_name, p.email AS prospect_email, p.company, "
            "p.status AS prospect_status FROM outbox o "
            "LEFT JOIN prospects p ON p.id = o.prospect_id "
            "WHERE o.kind = 'sequence' AND o.status = 'sent' ORDER BY o.sent_at"):
        pid = e.get("prospect_id")
        if not pid or pid in have:
            continue
        row = waiting.setdefault(pid, {
            "id": "awaiting-" + pid, "prospect_id": pid, "campaign_id": "",
            "channel": "email", "intent": "", "stage": "", "status": "awaiting_reply",
            "thread": [], "draft": None, "emails": [],
            "first_name": e.get("first_name") or "", "last_name": e.get("last_name") or "",
            "prospect_email": e.get("prospect_email") or "", "company": e.get("company") or "",
            "prospect_status": e.get("prospect_status") or "",
        })
        row["emails"].append({"step": e["step"], "subject": e["subject"],
                              "body": e["body"], "sent_at": e["sent_at"]})
        row["created_at"] = row.get("created_at") or e["sent_at"]
        row["updated_at"] = e["sent_at"]
    return sorted(waiting.values(), key=lambda r: r.get("updated_at") or "", reverse=True)


@app.post("/api/conversations/{convo_id}/status")
async def set_conversation_status(convo_id: str, request: Request):
    """Close a conversation, or hand one Harvey escalated back to it.

    Harvey answers new replies only on open conversations, so "open" means
    "Harvey, carry on" and "closed" means it's done."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    status = data.get("status") if isinstance(data, dict) else None
    if status not in ("open", "closed"):
        return JSONResponse({"success": False, "message": "status must be open or closed"},
                            status_code=400)
    rows = await query_db("SELECT status FROM conversations WHERE id = ?", (convo_id,))
    if not rows:
        return JSONResponse({"success": False, "message": "No such conversation."},
                            status_code=404)
    state = _state()
    await state.update_conversation(convo_id, status=status)
    await state.log_action("conversation_status", "dashboard",
                           {"conversation_id": convo_id, "from": rows[0]["status"], "to": status})
    return {"success": True, "status": status}


@app.get("/api/trend")
async def get_trend(days: int = 14, tz: int = 0):
    """Daily counts for the last `days` days in the viewer's time zone
    (`tz`: minutes east of UTC; stored times are UTC): contacts found,
    emails sent, first replies. Days with nothing are 0."""
    from datetime import datetime, timedelta, timezone
    days = max(1, min(90, days))
    tz = max(-840, min(840, tz))
    shift = f"{tz:+d} minutes"
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).date()
    labels = [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]

    async def per_day(table: str, column: str, where: str = "") -> list[int]:
        rows = await query_db(
            f"SELECT date({column}, ?) AS d, COUNT(*) AS n FROM {table} "
            f"WHERE date({column}, ?) >= ? {where} GROUP BY d", (shift, shift, labels[0]))
        counts = {r["d"]: r["n"] for r in rows}
        return [counts.get(day, 0) for day in labels]

    return {
        "days": labels,
        "found": await per_day("prospects", "created_at"),
        "sent": await per_day("outbox", "sent_at", "AND status = 'sent'"),
        "replies": await per_day("conversations", "created_at"),
    }


@app.get("/api/activity")
async def get_activity():
    rows = await query_db("SELECT * FROM actions ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
    return rows


# ── Dashboard UI ──


# ── Discovery: the provider menu, an estimate, and a run ──
#
# This is the only stage that spends money, so the UI never starts one without
# showing what it will cost first.

_discovery_task: asyncio.Task | None = None
_discovery_report: dict | None = None
_discovery_kind = "discover"   # or "profile": reading sites has no stop switch


def _discovery_queries(body: dict, config):
    from harvey.collectors.discover import build_queries, preset_problem

    preset = str(body.get("preset") or "")
    problem = preset_problem(preset, str(body.get("provider") or ""), config=config)
    if problem:
        raise ValueError(problem)
    cities = [c.strip() for c in (body.get("cities") or []) if c.strip()] or None
    return build_queries(
        config, cities=cities,
        depth=int(body.get("depth") or 30),
        limit=int(body.get("limit") or 100),
        preset=preset,
    )


@app.get("/api/discover/providers")
async def get_discovery_providers():
    """What each source does, what it costs, and whether it's ready to use."""
    try:
        from harvey.collectors.discover import DEFAULT_PROVIDER, provider_menu
        from harvey.config import load_env

        state = _state()
        await state.init_db()
        return {
            "providers": provider_menu(load_env().model_dump()),
            "default": DEFAULT_PROVIDER,
            "selected": await state.get_setting("discovery_provider") or DEFAULT_PROVIDER,
            "paused": await state.get_setting("discovery_paused"),
            "running": bool(_discovery_task and not _discovery_task.done()),
            "running_kind": _discovery_kind,
            "unprofiled": await state.count_companies_needing_profile(),
            "last_report": _discovery_report,
        }
    except Exception as e:
        logger.exception("discovery providers failed")
        return {"providers": [], "error": str(e)}


@app.post("/api/discover/estimate")
async def estimate_discovery(request: Request):
    """Projected spend and the exact query list, before anything is called."""
    try:
        from harvey.collectors.discover import PROVIDERS, estimate_cost
        from harvey.config import load_config

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"error": f"unknown provider {provider!r}"},
                                status_code=400)

        try:
            queries = _discovery_queries(body, load_config())
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {
            "provider": provider,
            "queries": [q.keyword() for q in queries],
            "query_count": len(queries),
            "estimated_cost": round(estimate_cost(provider, queries), 4),
            "free": PROVIDERS[provider].estimate(queries) == 0,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/discover/run")
async def start_discovery(request: Request):
    """Kick off a run in the background and hand back immediately.

    Discovery takes minutes, not milliseconds — holding the request open
    would just time out. Progress shows up in the run log.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from harvey.collectors.discover import PROVIDERS
        from harvey.config import load_config
        from harvey.pipeline import run_prospecting

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"success": False,
                                 "message": f"unknown provider {provider!r}"},
                                status_code=400)

        config = load_config()
        try:
            queries = _discovery_queries(body, config)
        except ValueError as e:
            return JSONResponse({"success": False, "message": str(e)}, status_code=400)
        max_spend = float(body.get("max_spend") or 1.0)

        state = _state()
        await state.init_db()
        await state.set_setting("discovery_provider", provider)
        await state.set_setting("discovery_paused", "")

        global _discovery_kind
        _discovery_kind = "discover"

        async def _go():
            global _discovery_report
            try:
                # Discovery chains straight into profiling: reading the sites
                # is free, and it is what makes the results worth anything.
                result = await run_prospecting(state, config, provider, queries,
                                               max_spend=max_spend)
                _discovery_report = {
                    **(result.discover or {}),
                    "profiled_companies": result.profiled_companies,
                    "profile_observations": result.profile_observations,
                    "errors": result.errors,
                }
            except Exception as exc:
                logger.exception("discovery run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "queries": len(queries)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/profile/run")
async def start_profile():
    """Read the websites of everything discovered but not yet looked at.

    Free and model-free, so there is nothing to estimate and no cap to set.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from harvey.pipeline import run_profile_stage

        state = _state()
        await state.init_db()
        pending = await state.count_companies_needing_profile()
        if not pending:
            return {"success": True, "pending": 0}

        async def _go():
            global _discovery_report
            try:
                try:
                    from harvey.config import load_config
                    config = load_config()
                except Exception:
                    config = None   # still profiles; no role is singled out
                companies, observations, _ = await run_profile_stage(
                    state, limit=200, config=config)
                _discovery_report = {
                    "profiled_companies": companies,
                    "profile_observations": observations,
                }
            except Exception as exc:
                logger.exception("profile run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        global _discovery_kind
        _discovery_kind = "profile"
        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "pending": pending}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/discover/stop")
async def stop_discovery():
    """Kill switch. Read between batches, so an in-flight run stops cleanly."""
    try:
        state = _state()
        await state.init_db()
        await state.set_setting("discovery_paused", "stopped from dashboard")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


def _parse_ts(value) -> datetime | None:
    """SQLite CURRENT_TIMESTAMP / isoformat strings -> naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").split("+")[0])
    except ValueError:
        return None


async def _scalar(sql: str, params: tuple = ()) -> int:
    rows = await query_db(sql, params)
    return int(next(iter(rows[0].values())) or 0) if rows else 0


async def _first(sql: str, params: tuple = (), key: str = "t"):
    """First row's `key`, or None — query_db returns [] on any DB problem."""
    rows = await query_db(sql, params)
    return rows[0].get(key) if rows else None


# A run row still says 'running' after its process was killed (closed window,
# reboot). No new facts for this long means it is not coming back.
STALE_RUN_SECONDS = 900
# Contact-finding has no run row; a recent prospects_checked_at means it is active.
CONTACT_ACTIVE_SECONDS = 180
# Moved on from "waiting to be emailed" (opted_out too: never offered again).
SENT_STATUSES = ("contacted", "replied", "meeting", "closed", "lost", "opted_out")
REPLY_STATUSES = ("replied", "meeting", "closed")


def _eta_text(minutes) -> str:
    if minutes is None:
        return ""
    return "less than a minute left" if minutes < 1 else f"about {round(minutes)} min left"


@app.get("/api/live-status")
async def get_live_status():
    """Plain-language progress: each pipeline step, what is running, how long is left.

    Built for someone who would otherwise keep asking "what's the status?".
    Everything is derived from the database, so it is true whatever started
    the work — `harvey run`, a CLI command, or a one-off script. Every total
    counts only what the step can act on (a business with no website is never
    read), so a finished step always reaches 100% instead of sticking.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # DB stores naive UTC
    has_site = "COALESCE(domain, '') != ''"
    companies = await _scalar("SELECT COUNT(*) FROM companies")
    with_site = await _scalar(f"SELECT COUNT(*) FROM companies WHERE {has_site}")
    no_site = companies - with_site
    profiled = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at IS NOT NULL")
    checked = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} "
        "AND profile_checked_at IS NOT NULL AND prospects_checked_at IS NOT NULL")

    prospects = await query_db("SELECT id, email, email_status, status FROM prospects")
    with_email = [p for p in prospects if p.get("email")]
    by_status: dict[str, int] = {}
    for p in with_email:
        k = (p.get("email_status") or "guess").lower()
        by_status[k] = by_status.get(k, 0) + 1
    deliverable = {p["id"] for p in with_email
                   if (p.get("email_status") or "") in ("verified", "risky")}
    unconfirmed = sum(n for k, n in by_status.items()
                      if k not in ("verified", "risky", "invalid"))

    # Drafts are counted per person, not per campaign: one campaign can cover many.
    drafted: set[str] = set()
    for row in await query_db("SELECT prospect_ids_json FROM campaigns"):
        try:
            ids = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            ids = []
        if isinstance(ids, list):
            drafted.update(i for i in ids if isinstance(i, str))
    sent = {p["id"] for p in prospects if p.get("status") in SENT_STATUSES}
    # Who is left for the user to send by hand: only when Harvey can't send
    # itself, and never someone Harvey has queued, sent to, or can't reach.
    harvey_sends = _harvey_can_send()
    awaiting_review = await _scalar("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'")
    by_hand: set[str] = set()
    if not harvey_sends:
        skip = await _harvey_committed_ids() | await _hand_sent_ids()
        by_hand = {p["id"] for p in prospects
                   if p["id"] in drafted and p["id"] not in skip
                   and (p.get("status") or "new") in HAND_SENDABLE_STATUSES
                   and (p.get("email_status") or "guess") in HAND_SENDABLE_EMAIL}
    replies = sum(1 for p in prospects if p.get("status") in REPLY_STATUSES)
    cost = float(await _first(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs", key="c") or 0)

    running, stopped = [], []
    for run in await query_db(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at"):
        started = _parse_ts(run.get("started_at"))
        elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0
        last = _parse_ts(await _first(
            "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
            (run["id"],))) or started
        item = {"stage": run.get("stage"), "provider": run.get("provider"),
                "elapsed_min": round(elapsed / 60, 1), "eta_min": None}
        if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
            stopped.append(item)
            continue
        if run.get("stage") == "profile" and started:
            done_in_run = await _scalar(
                f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at >= ?",
                (run["started_at"],))
            left = max(0, with_site - profiled)
            if left and done_in_run and elapsed > 0:
                item["eta_min"] = left / (done_in_run / elapsed) / 60
        running.append(item)

    last_contact = _parse_ts(await _first(
        "SELECT MAX(prospects_checked_at) AS t FROM companies"))
    finding_emails = bool(
        last_contact and (now - last_contact).total_seconds() < CONTACT_ACTIVE_SECONDS)
    reading = any(r["stage"] == "profile" for r in running)
    finding = any(r["stage"] == "discover" for r in running)

    def step(label, done, total, active, detail, info=False):
        if active:
            state_ = "running"
        elif info:
            state_ = "done" if done else "waiting"
        elif total and done >= total:
            state_ = "done"
        elif done:
            state_ = "partial"
        else:
            state_ = "waiting"
        return {"label": label, "done": done, "total": total,
                "state": state_, "detail": detail}

    profile_eta = next((r["eta_min"] for r in running if r["stage"] == "profile"), None)
    read_detail = f"{profiled} of {with_site} read"
    if reading and profile_eta is not None:
        read_detail += " · " + _eta_text(profile_eta)
    if no_site:
        read_detail += f" · {no_site} have no website (skipped)"

    steps = [
        step("Find businesses", companies, 0, finding,
             f"{companies} businesses found", info=True),
        step("Read their websites", profiled, with_site, reading, read_detail),
        step("Find emails", checked, profiled, finding_emails,
             f"{checked} of {profiled} checked · {len(with_email)} emails found"),
        step("Check emails are real", len(with_email) - unconfirmed, len(with_email), False,
             f"{by_status.get('verified', 0)} verified · {by_status.get('risky', 0)} risky · "
             f"{unconfirmed} not confirmed"
             + (f" · {by_status['invalid']} bad" if by_status.get("invalid") else "")),
        step("Write first emails", len(drafted), len(deliverable | drafted), False,
             f"{len(drafted)} written · see Campaigns"),
        step("Sent", len(sent & drafted), len(drafted), False,
             f"{len(sent & drafted)} of {len(drafted)} sent"),
        step("Replies", replies, 0, False,
             f"{replies} replies" if replies else "No replies yet", info=True),
    ]

    next_tab = ""   # where the next step is done, when it's done in the dashboard
    if reading or finding:
        next_up = "Wait — Harvey is working. This page updates by itself."
    elif finding_emails:
        next_up = "Wait — emails are being found."
    elif stopped:
        next_up, next_tab = "A job stopped before finishing. Run it again.", "discover"
    elif harvey_sends and awaiting_review:
        next_up, next_tab = f"Review the {awaiting_review} draft(s) waiting in the Outbox.", "outbox"
    elif by_hand:
        next_up, next_tab = f"Open Campaigns and send the {len(by_hand)} unsent email(s).", "campaigns"
    elif profiled > checked:
        next_up, next_tab = f"Find emails for the {profiled - checked} businesses not checked yet.", "controls"
    elif deliverable - drafted:
        next_up, next_tab = f"Write first emails for {len(deliverable - drafted)} more contact(s).", "controls"
    elif not companies:
        next_up, next_tab = "Search for businesses to get started.", "discover"
    else:
        next_up, next_tab = "Search more cities or industries to get more leads.", "discover"

    return {
        "steps": steps,
        "running": [dict(r, eta_text=_eta_text(r["eta_min"])) for r in running],
        "stopped": stopped,
        "next": next_up,
        "next_tab": next_tab,
        "cost_usd": round(cost, 4),
        "updated_at": now.isoformat() + "Z",
    }


def _require_approval_now() -> bool:
    try:
        from harvey.config import load_config
        return bool(load_config(str(_resolve_config_file())).channels.email.require_approval)
    except Exception:
        return True


@app.get("/api/today")
async def get_today():
    """What needs a human, right now.

    The dashboard opens here rather than on a setup checklist: the question a
    returning user actually has is "is anything waiting on me?", and the
    answer is usually a short list or nothing at all.
    """
    items: list[dict] = []
    stats: dict = {}
    try:
        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        proposed = [c for c in codes if c.get("status") == "proposed"]
        confirmed = [c for c in codes if c.get("status") == "confirmed"]
        pending = await state.get_outbox(status="pending_review", limit=200)
        approved = await state.get_outbox(status="approved", limit=200)
        paused = await state.get_setting("sending_paused")
        counts = await state.count_prospects_by_status()

        convos = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'open'"
        )
        open_convos = convos[0]["n"] if convos else 0
        handed = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'needs_human'"
        )
        needs_human = handed[0]["n"] if handed else 0
        companies = await query_db("SELECT COUNT(*) AS n FROM companies")
        n_companies = companies[0]["n"] if companies else 0

        unprofiled_count = await state.count_companies_needing_profile()
        stats = {
            "companies": n_companies,
            "prospects": sum(counts.values()),
            "signals_confirmed": len(confirmed),
            "outbox_pending": len(pending),
            "outbox_approved": len(approved),
            "open_conversations": open_convos,
            "needs_human": needs_human,
            "signals_proposed": len(proposed),
            "email_provider": _current_provider(),
            "unprofiled": unprofiled_count,
        }

        # Ordered by how much it blocks Harvey from doing anything at all.
        brain = await _brain_status()
        if brain.get("status") == "paused":
            who = "Codex" if brain.get("provider") == "codex" else "Harvey's model"
            items.append({
                "key": "brain-paused", "tone": "bad",
                "title": f"{who} is paused, so Harvey is waiting",
                "detail": (str(brain.get("reason") or "It can't be used right now.")[:300] +
                           " Replies and queued emails are untouched; Harvey resumes "
                           "once it's fixed. harvey status --check says what's wrong."),
                "action": "Open the log", "tab": "controls",
            })

        hold = await state.current_sends_held()
        if hold:
            from harvey.sending_mode import held_message
            title, detail = held_message(hold, waiting=len(approved))
            items.append({
                "key": "sends-held", "tone": "bad", "title": title, "detail": detail,
                "action": "Open the log", "tab": "controls",
            })

        if paused:
            items.append({
                "key": "paused", "tone": "bad",
                "title": "Sending is paused",
                "detail": str(paused) + ". Nothing will go out until you resume it.",
                "action": "Review and resume", "tab": "outbox",
            })

        # Cold email waiting on the optional Precleared check: its free
        # checks ran out, the key was rejected, or it couldn't be reached.
        from harvey.integrations.precleared import current_hold, hold_message
        try:
            from harvey.config import load_config
            loaded = load_config(str(_resolve_config_file()))
        except Exception:
            loaded = None  # can't judge: show the hold rather than hide it
        precleared_hold = await current_hold(state, loaded)
        if precleared_hold:
            message = hold_message(precleared_hold)
            items.append({
                "key": "precleared-hold", "tone": message["tone"],
                "title": message["title"], "detail": message["detail"],
                "action": "Open the log", "tab": "controls",
                "url": message["url"], "url_label": message["url_label"],
            })

        setup = await get_setup_status()
        if isinstance(setup, dict) and setup.get("percent", 100) < 100:
            missing = [c["label"] for c in setup.get("checks", [])
                       if c.get("required") and not c.get("done")]
            items.append({
                "key": "setup", "tone": "warn",
                "title": "Finish setting Harvey up",
                "detail": ", ".join(missing[:3]) or "Some required steps are incomplete.",
                "action": "Open setup", "tab": "settings",
            })

        if proposed:
            items.append({
                "key": "signals", "tone": "warn",
                "title": (f"{len(proposed)} signal waiting for your confirmation"
                          if len(proposed) == 1
                          else f"{len(proposed)} signals waiting for your confirmation"),
                "detail": ("Harvey won't collect anything you haven't approved. "
                           "Confirm which signals define a good prospect for you."),
                "action": "Review signals", "tab": "signals",
            })
        elif not confirmed:
            items.append({
                "key": "signals-none", "tone": "warn",
                "title": "No signals confirmed",
                "detail": "Every signal is rejected, so prospecting has nothing to collect.",
                "action": "Review signals", "tab": "signals",
            })

        if not n_companies and confirmed:
            items.append({
                "key": "discover", "tone": "good",
                "title": "No companies yet",
                "detail": ("Signals are confirmed but nothing has been collected. "
                           "Discovery is free to try — no account needed."),
                "action": "Find businesses", "tab": "discover",
            })

        unprofiled = unprofiled_count
        if unprofiled:
            items.append({
                "key": "profile", "tone": "good",
                "title": f"{unprofiled} companies not looked at yet",
                "detail": ("Reading their websites is free and it is what makes "
                           "an email specific — who their agency is, what they "
                           "are missing, whether they are spending on ads."),
                "action": "Read their sites", "tab": "discover",
            })

        if needs_human:
            items.append({
                "key": "needs-human", "tone": "bad",
                "title": (f"{needs_human} conversation needs you" if needs_human == 1
                          else f"{needs_human} conversations need you"),
                "detail": ("Harvey stopped and handed these over: an escalation, or a "
                           "reply it had no safe answer for. Reply yourself, then close "
                           "it or hand it back."),
                "action": "Read them", "tab": "conversations",
            })

        if pending:
            items.append({
                "key": "outbox", "tone": "warn",
                "title": (f"{len(pending)} email waiting for approval" if len(pending) == 1
                          else f"{len(pending)} emails waiting for approval"),
                "detail": ("Nothing sends until you approve it. Read them one at a time."
                           if _require_approval_now() else
                           "Autopilot sends the rest on its own; these wait for you. "
                           "Read them one at a time."),
                "action": "Open the decisions desk", "tab": "outbox",
            })

        if open_convos:
            items.append({
                "key": "replies", "tone": "good",
                "title": (f"{open_convos} live conversation" if open_convos == 1
                          else f"{open_convos} live conversations"),
                "detail": "People replied. Check how Harvey is handling them.",
                "action": "Read conversations", "tab": "conversations",
            })

        return {"items": items, "stats": stats}
    except Exception as e:
        logger.exception("today load failed")
        return {"items": [], "stats": stats, "error": str(e)}


# ── Signals: Harvey proposes, the user confirms ──
#
# Nothing is collected until a human has said yes to it. This is the gate the
# whole prospecting pipeline hangs off: collectors ask `state.confirmed_signal_codes()`
# and skip anything that isn't in the set.

CATEGORY_META = {
    "discovery": {
        "label": "Discovery — who exists",
        "blurb": "How Harvey finds businesses at all, and how visible they are. "
                 "This is the only stage that costs money.",
    },
    "profile": {
        "label": "Profile — what they are",
        "blurb": "Read from the pages a business already publishes. Free, no AI "
                 "tokens, three HTTP requests per company. These are the signals "
                 "that make an email specific.",
    },
    "people": {
        "label": "People — who decides",
        "blurb": "Named humans and whether they're the one who can say yes.",
    },
    "verification": {
        "label": "Contactability — can you reach them",
        "blurb": "Whether the address will actually deliver, and what to do when "
                 "it won't.",
    },
}
CATEGORY_ORDER = ["discovery", "profile", "people", "verification"]


@app.get("/api/signals")
async def get_signals():
    """The signal vocabulary, grouped for review, with live cohort sizes."""
    try:
        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seeding is idempotent and never overrides a decision the user made,
        # so it is safe to run on every load — new signals shipped in an
        # upgrade show up as `proposed` without any migration step.
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}

        groups, summary = [], {"proposed": 0, "confirmed": 0, "rejected": 0}
        for cat in CATEGORY_ORDER:
            rows = []
            for sig in codes:
                if sig.get("category") != cat:
                    continue
                seen = counts.get(sig["code"], {})
                rows.append({
                    **sig,
                    "companies": seen.get("companies", 0),
                    "observations": seen.get("observations", 0),
                })
            if rows:
                meta = CATEGORY_META.get(cat, {})
                groups.append({
                    "key": cat,
                    "label": meta.get("label", cat.title()),
                    "blurb": meta.get("blurb", ""),
                    "signals": rows,
                })
        for sig in codes:
            summary[sig.get("status", "proposed")] = (
                summary.get(sig.get("status", "proposed"), 0) + 1
            )

        return {"summary": summary, "groups": groups, "total": len(codes)}
    except Exception as e:
        logger.exception("signals load failed")
        return {"error": str(e), "groups": [], "summary": {}}


@app.post("/api/signals/status")
async def set_signals_status(request: Request):
    """Confirm or reject one signal, or a whole category at once."""
    try:
        body = await request.json()
        status = (body.get("status") or "").strip()
        codes = body.get("codes") or ([body["code"]] if body.get("code") else [])
        if status not in ("proposed", "confirmed", "rejected"):
            return JSONResponse(
                {"success": False, "message": f"invalid status: {status!r}"},
                status_code=400,
            )
        if not codes:
            return JSONResponse(
                {"success": False, "message": "no signal codes given"}, status_code=400
            )

        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seed first: a confirm that arrives before anything has loaded the
        # catalog would otherwise report success while changing nothing.
        await seed_signal_catalog(state)

        changed, unknown = 0, []
        for code in codes:
            if await state.set_signal_status(code, status):
                changed += 1
            else:
                unknown.append(code)
        return {
            "success": True, "changed": changed,
            "status": status, "unknown": unknown,
        }
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


COHORT_PAGE_SIZE = 50
COHORT_MAX_PAGE_SIZE = 200


async def _cohort_companies(require: list[str], exclude: list[str],
                            offset: int = 0, limit: int | None = None,
                            ranges: list[dict] | None = None):
    state = _state()
    await state.init_db()
    return await state.cohort_companies(require, exclude, offset, limit,
                                        ranges=ranges)


def _codes(raw) -> list[str]:
    """Accept lists or CSV strings, with bounded SQL parameter counts."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or any(not isinstance(c, str) for c in raw):
        raise ValueError("signals must be a list of strings or comma-separated string")
    codes = list(dict.fromkeys(c.strip() for c in raw if c.strip()))
    if len(codes) > 200 or any(len(c) > 128 for c in codes):
        raise ValueError("too many or oversized signal codes")
    return codes


COHORT_MAX_RANGES = 20


def _range_bound(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("range bounds must be numbers")
    value = float(raw)
    if value != value or abs(value) > 1e12:
        raise ValueError("range bound is out of bounds")
    return value


def _ranges(raw) -> list[dict]:
    """Numeric cohort filters: a list of {code, min, max}, or the CSV query
    form "SERP_RANK:11:30,MAPS_RANK::3". At least one bound per range."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        items = []
        for part in raw.split(","):
            if not part.strip():
                continue
            pieces = part.split(":")
            if len(pieces) != 3:
                raise ValueError("ranges look like CODE:min:max")
            items.append({"code": pieces[0], "min": pieces[1], "max": pieces[2]})
        raw = items
    if not isinstance(raw, list) or any(not isinstance(r, dict) for r in raw):
        raise ValueError("ranges must be a list of {code, min, max}")
    if len(raw) > COHORT_MAX_RANGES:
        raise ValueError("too many ranges")
    out = []
    for r in raw:
        code = r.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 128:
            raise ValueError("each range needs a signal code")
        low, high = _range_bound(r.get("min")), _range_bound(r.get("max"))
        if low is None and high is None:
            raise ValueError(f"range on {code} needs a min or a max")
        if low is not None and high is not None and low > high:
            raise ValueError(f"range on {code}: min is above max")
        out.append({"code": code.strip(), "min": low, "max": high})
    return out


def _cohort_integer(raw, default):
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError("offset and limit must be integers")
    value = int(raw)
    if abs(value) > 9223372036854775807:
        raise ValueError("offset or limit is too large")
    return value


@app.post("/api/cohort")
async def preview_cohort(request: Request):
    """How many companies carry ALL these signals and none of those.

    The payoff for confirming signals: a cohort is a query, not a list. Set
    intersection happens in SQL — intersecting in JS over a capped fetch
    silently returns the wrong answer. ``offset``/``limit`` page through the
    matches; ``size`` is always the whole cohort. ``ranges`` adds numeric
    filters — ``[{"code": "SERP_RANK", "min": 11, "max": 30}]``.
    """
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        require = _codes(body.get("require"))
        exclude = _codes(body.get("exclude"))
        ranges = _ranges(body.get("ranges"))
        offset = max(0, _cohort_integer(body.get("offset"), 0))
        limit = min(max(1, _cohort_integer(body.get("limit"), COHORT_PAGE_SIZE)),
                    COHORT_MAX_PAGE_SIZE)
        if not require and not ranges:
            return {"size": 0, "offset": 0, "limit": limit, "companies": []}

        size, rows = await _cohort_companies(require, exclude, offset, limit,
                                             ranges=ranges)
        return {"size": size, "offset": offset, "limit": limit,
                "companies": rows}
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"size": 0, "companies": [], "error": str(e)}, status_code=500)


def _csv_cell(value):
    text = str(value or "")
    # Spreadsheet software may ignore leading whitespace before formulas.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@app.get("/api/cohort.csv")
async def export_cohort(require: str = "", exclude: str = "", ranges: str = ""):
    """The whole cohort as a CSV download — every match, not just one page."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "domain", "website", "industry", "location"])
    try:
        codes = _codes(require)
        excluded = _codes(exclude)
        bounds = _ranges(ranges)
        if codes or bounds:
            _, rows = await _cohort_companies(codes, excluded, ranges=bounds)
            for r in rows:
                writer.writerow([_csv_cell(r.get(k)) for k in
                                 ("name", "domain", "website", "industry", "location")])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("Cohort export failed: %s", e)
        return JSONResponse({"error": "Cohort export failed"}, status_code=500)
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cohort.csv"'},
    )


@app.get("/api/runs")
async def get_runs_api():
    """The collector run log — what ran, when, what it produced and cost."""
    try:
        state = _state()
        await state.init_db()
        await state.sweep_stale_runs()
        runs = await state.get_runs(limit=25)
        # Same rule as the live panel: a "running" run that has recorded
        # nothing for STALE_RUN_SECONDS has stopped, whatever its row says.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for run in runs:
            if run.get("status") != "running":
                continue
            last = _parse_ts(await _first(
                "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
                (run["id"],))) or _parse_ts(run.get("started_at"))
            if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
                run["status"] = "stalled"
        return runs
    except Exception as e:
        return {"error": str(e)}


WEB_DIR = (Path(__file__).resolve().parent / "web")


TEXT_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
BINARY_TYPES = {".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png"}


@app.get("/static/{path:path}")
async def static_file(path: str):
    """Serve the dashboard's own assets from disk.

    Read per-request rather than cached at import: editing app.css and hitting
    reload is the whole point of having them as real files. Fonts are vendored
    rather than fetched from a CDN — this is a local tool and it should work
    with the network off.
    """
    target = (WEB_DIR / path).resolve()
    root = WEB_DIR.resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return PlainTextResponse("not found", status_code=404)

    if target.suffix in BINARY_TYPES:
        return Response(
            target.read_bytes(),
            media_type=BINARY_TYPES[target.suffix],
            headers={"Cache-Control": "public, max-age=604800"},
        )
    return PlainTextResponse(
        target.read_text(encoding="utf-8"),
        media_type=TEXT_TYPES.get(target.suffix, "text/plain"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


class DashboardSetupError(Exception):
    """The dashboard can't start safely; the message says what to do."""


def _check_loopback_bind(host: str) -> str:
    """The host to bind, which must be a loopback address. The dashboard has
    no remote-access mode: from another machine, use an SSH tunnel."""
    name = (host or "").strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not auth.is_loopback(name):
        raise DashboardSetupError(
            f"Refusing to listen on {host!r}: the dashboard only listens on this machine "
            "(127.0.0.1, ::1 or localhost). To reach it from elsewhere, tunnel instead: "
            "ssh -L 5555:127.0.0.1:5555 <this-host>")
    return name


def dashboard_url(host: str = "127.0.0.1", port: int = 5555) -> str:
    """The unlock link. The token rides in the #fragment, which browsers
    never send to the server (so it stays out of logs and Referer); the
    page moves it to sessionStorage and strips it from the address bar."""
    name = _check_loopback_bind(host)
    try:
        if ipaddress.ip_address(name).version == 6:
            name = f"[{name}]"
    except ValueError:
        pass
    return f"http://{name}:{port}/#token={token_store.get()}"


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server (loopback only, token required)."""
    import uvicorn

    try:
        bind = _check_loopback_bind(host)
        url = dashboard_url(host, port)
    except (DashboardSetupError, auth.TokenError) as e:
        raise DashboardSetupError(str(e)) from e

    print(f"\n  Harvey Dashboard running. Open this link to unlock it:\n\n    {url}\n")
    print(f"  The token is stored in {token_store.path} (owner-only). Keep the link")
    print("  private; anyone with it can use the dashboard. Press Ctrl+C to stop.\n")
    if os.name == "nt":
        print("  Windows: the token file is protected by your folder's permissions,")
        print("  not POSIX file modes.\n")
    uvicorn.run(app, host=bind, port=port, log_level="warning",
                proxy_headers=False, server_header=False, date_header=False)
