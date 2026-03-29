"""Harvey Dashboard — local web UI to set up, control, and monitor Harvey."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("harvey.dashboard")

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "harvey.db"
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "harvey.yaml"
PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"
LOG_FILE = PROJECT_ROOT / "data" / "harvey.log"

app = FastAPI(title="Harvey Dashboard")

# Harvey process tracking
_harvey_process: subprocess.Popen | None = None
_harvey_started_at: datetime | None = None
_env_lock = asyncio.Lock()


# ── Helpers ──


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts."""
    if not DB_PATH.exists():
        return []
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****" if key else ""
    return key[:4] + "****" + key[-4:]


def _read_env_file() -> dict[str, str]:
    """Read .env file and return as dict."""
    env_vars = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def _write_env_file(updates: dict[str, str]):
    """Update .env file with new values, preserving existing entries."""
    existing = _read_env_file()
    existing.update(updates)
    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    load_dotenv(str(ENV_FILE), override=True)


def _check_harvey_pid() -> int | None:
    """Check if there's a running Harvey process from a PID file."""
    global _harvey_process, _harvey_started_at
    if _harvey_process and _harvey_process.poll() is None:
        return _harvey_process.pid
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            PID_FILE.unlink(missing_ok=True)
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

    # 3. Instantly API key
    instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
    instantly_set = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
    checks.append({
        "id": "instantly_key", "label": "Instantly API key",
        "done": instantly_set,
        "required": True,
        "help": "Get your API key from Instantly Settings > Integrations. Enter it in the Settings tab.",
    })

    # 4. Instantly API working
    instantly_works = False
    if instantly_set:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    "https://api.instantly.ai/api/v2/accounts",
                    headers={"Authorization": f"Bearer {instantly_key}"},
                )
                instantly_works = resp.status_code == 200
        except Exception:
            pass
    checks.append({
        "id": "instantly_works", "label": "Instantly API connected",
        "done": instantly_works,
        "required": True,
        "help": "Your Instantly API key isn't working. Check that it's correct and you have the Growth plan.",
    })

    # 5. Config valid
    config_valid = False
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f)
            company = cfg.get("persona", {}).get("company", "")
            product = cfg.get("product", {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": "Harvey configured (harvey.yaml)",
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

    required_checks = [c for c in checks if c["required"]]
    completed_required = sum(1 for c in required_checks if c["done"])

    return {
        "checks": checks,
        "completed": completed_required,
        "total_required": len(required_checks),
        "percent": int(completed_required / len(required_checks) * 100) if required_checks else 0,
    }


# ── Settings ──


@app.get("/api/settings")
async def get_settings():
    """Get current settings (API keys masked)."""
    env_vars = _read_env_file()
    # Also check os.environ as fallback
    for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"]:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    return {
        "instantly_api_key": env_vars.get("INSTANTLY_API_KEY", ""),
        "instantly_api_key_masked": _mask_key(env_vars.get("INSTANTLY_API_KEY", "")),
        "linkedin_email": env_vars.get("LINKEDIN_EMAIL", ""),
        "linkedin_password_set": bool(env_vars.get("LINKEDIN_PASSWORD", "")),
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_masked": _mask_key(env_vars.get("CLOUDFLARE_API_TOKEN", "")),
    }


@app.post("/api/settings/env")
async def save_env_settings(request: Request):
    """Save environment variables to .env file."""
    data = await request.json()
    async with _env_lock:
        updates = {}
        for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                     "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"]:
            if key in data and data[key] is not None:
                updates[key] = data[key]
        if updates:
            _write_env_file(updates)
    return {"success": True}


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key."""
    data = await request.json()
    api_key = data.get("api_key", "")
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
    data = await request.json()
    entity_type = data.get("entity_type", "")
    entity_id = data.get("entity_id", "")
    comment = data.get("comment", "")
    if not comment:
        return {"success": False, "message": "Comment is required."}
    feedback_id = uuid.uuid4().hex[:12]
    db_path = str(DB_PATH)
    if DB_PATH.exists():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO feedback (id, entity_type, entity_id, comment) VALUES (?, ?, ?, ?)",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
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

    # Ensure data dir exists
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

    log_handle = open(LOG_FILE, "a")
    _harvey_process = subprocess.Popen(
        [sys.executable, "-m", "harvey"],
        cwd=str(PROJECT_ROOT),
        stdout=log_handle,
        stderr=log_handle,
        start_new_session=True,
    )
    _harvey_started_at = datetime.now()

    # Write PID file
    PID_FILE.write_text(str(_harvey_process.pid))

    return {"success": True, "pid": _harvey_process.pid}


@app.post("/api/harvey/stop")
async def stop_harvey():
    """Stop the Harvey subprocess."""
    global _harvey_process, _harvey_started_at

    pid = _check_harvey_pid()
    if not pid:
        return {"success": False, "message": "Harvey is not running."}

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            try:
                os.kill(pid, 0)
                await asyncio.sleep(0.5)
            except ProcessLookupError:
                break
        else:
            # Force kill if still running
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except ProcessLookupError:
        pass

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
        text = LOG_FILE.read_text()
        lines = text.strip().splitlines()[-50:]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


# ── Pipeline Data (existing endpoints) ──


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

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        usage = await query_db(
            "SELECT claude_calls FROM usage_log WHERE date = date('now')"
        )
        usage_today = usage[0]["claude_calls"] if usage else 0

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "actions_total": action_count,
            "claude_calls_today": usage_today,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/prospects")
async def get_prospects():
    rows = await query_db("SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200")
    return rows


@app.get("/api/campaigns")
async def get_campaigns():
    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
    return rows


@app.get("/api/conversations")
async def get_conversations():
    rows = await query_db("""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
    return rows


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


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Harvey Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0a0a0a;
    color: #e0e0e0;
    min-height: 100vh;
  }

  header {
    background: #111;
    border-bottom: 1px solid #222;
    padding: 16px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  header h1 { font-size: 20px; font-weight: 600; color: #fff; }
  header h1 span { color: #666; font-weight: 400; font-size: 14px; margin-left: 8px; }

  .header-controls { display: flex; align-items: center; gap: 12px; }

  .harvey-status {
    display: flex; align-items: center; gap: 8px;
    font-size: 13px; color: #888;
    padding: 6px 14px;
    background: #1a1a1a;
    border: 1px solid #333;
    border-radius: 6px;
  }

  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
  }
  .status-dot.running { background: #40c060; box-shadow: 0 0 6px #40c060; }
  .status-dot.stopped { background: #666; }

  .refresh-btn {
    background: #1a1a1a; border: 1px solid #333; color: #888;
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
  }
  .refresh-btn:hover { border-color: #555; color: #ccc; }

  nav {
    background: #111; border-bottom: 1px solid #222;
    padding: 0 32px; display: flex; gap: 0; overflow-x: auto;
  }

  nav button {
    background: none; border: none; border-bottom: 2px solid transparent;
    color: #777; padding: 12px 16px; cursor: pointer; font-size: 13px;
    transition: all 0.15s; white-space: nowrap;
  }
  nav button:hover { color: #bbb; }
  nav button.active { color: #fff; border-bottom-color: #fff; }

  main { padding: 24px 32px; max-width: 1400px; }

  .section { display: none; }
  .section.active { display: block; }

  /* ── Cards ── */
  .card {
    background: #141414; border: 1px solid #222; border-radius: 10px;
    padding: 24px; margin-bottom: 16px;
  }
  .card h2 { font-size: 16px; color: #fff; margin-bottom: 16px; }
  .card h3 { font-size: 14px; color: #ccc; margin-bottom: 12px; }

  /* ── Stats ── */
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px; margin-bottom: 32px;
  }
  .stat-card { background: #141414; border: 1px solid #222; border-radius: 10px; padding: 20px; }
  .stat-card .label { font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #666; margin-bottom: 8px; }
  .stat-card .value { font-size: 32px; font-weight: 700; color: #fff; }
  .stat-card .breakdown { margin-top: 10px; font-size: 12px; color: #555; line-height: 1.6; }

  /* ── Progress bar ── */
  .progress-wrap { margin-bottom: 24px; }
  .progress-label { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 13px; }
  .progress-label .pct { color: #fff; font-weight: 600; }
  .progress-label .text { color: #666; }
  .progress-bar { background: #1a1a1a; border-radius: 8px; height: 12px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 8px; transition: width 0.5s ease; }
  .progress-fill.green { background: linear-gradient(90deg, #2d8a4e, #40c060); }
  .progress-fill.yellow { background: linear-gradient(90deg, #a08020, #d0b040); }

  /* ── Setup checklist ── */
  .check-item {
    display: flex; align-items: flex-start; gap: 12px;
    padding: 14px 0; border-bottom: 1px solid #1a1a1a;
  }
  .check-item:last-child { border-bottom: none; }
  .check-icon { font-size: 16px; min-width: 24px; text-align: center; padding-top: 1px; }
  .check-icon.done { color: #40c060; }
  .check-icon.pending { color: #555; }
  .check-info { flex: 1; }
  .check-label { font-size: 14px; color: #ddd; margin-bottom: 2px; }
  .check-label.done { color: #888; }
  .check-help { font-size: 12px; color: #555; margin-top: 4px; }
  .optional-tag { font-size: 10px; background: #1a1a2a; color: #8080ff; padding: 2px 8px; border-radius: 4px; margin-left: 8px; }

  /* ── Forms ── */
  .form-group { margin-bottom: 16px; }
  .form-label { display: block; font-size: 12px; color: #888; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.3px; }
  .form-input {
    width: 100%; padding: 10px 14px; background: #1a1a1a; border: 1px solid #333;
    border-radius: 6px; color: #e0e0e0; font-size: 14px; font-family: inherit;
  }
  .form-input:focus { outline: none; border-color: #555; }
  .form-input::placeholder { color: #444; }
  .form-row { display: flex; gap: 12px; align-items: flex-end; }
  .form-row .form-group { flex: 1; }
  .form-hint { font-size: 11px; color: #555; margin-top: 4px; }

  .btn {
    padding: 8px 20px; border: none; border-radius: 6px;
    font-size: 13px; cursor: pointer; font-family: inherit;
    transition: all 0.15s;
  }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-primary { background: #2d8a4e; color: #fff; }
  .btn-primary:hover:not(:disabled) { background: #38a05c; }
  .btn-danger { background: #8a2d2d; color: #fff; }
  .btn-danger:hover:not(:disabled) { background: #a03838; }
  .btn-secondary { background: #1a1a1a; border: 1px solid #333; color: #aaa; }
  .btn-secondary:hover:not(:disabled) { border-color: #555; color: #ddd; }
  .btn-sm { padding: 6px 14px; font-size: 12px; }

  .btn-group { display: flex; gap: 10px; margin-top: 16px; }

  .test-result { font-size: 13px; margin-top: 8px; padding: 8px 12px; border-radius: 6px; }
  .test-result.success { background: #152a1a; color: #40c060; }
  .test-result.error { background: #2a1515; color: #e05050; }

  /* ── Controls ── */
  .control-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 800px) { .control-panel { grid-template-columns: 1fr; } }

  .status-big { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
  .status-big .dot { width: 14px; height: 14px; border-radius: 50%; }
  .status-big .dot.running { background: #40c060; box-shadow: 0 0 8px #40c060; }
  .status-big .dot.stopped { background: #666; }
  .status-big .label { font-size: 18px; font-weight: 600; }
  .status-big .label.running { color: #40c060; }
  .status-big .label.stopped { color: #888; }
  .status-meta { font-size: 12px; color: #555; margin-bottom: 20px; }

  .log-viewer {
    background: #0a0a0a; border: 1px solid #222; border-radius: 8px;
    padding: 16px; font-family: 'SF Mono', Monaco, 'Consolas', monospace;
    font-size: 11px; color: #888; line-height: 1.6;
    max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
  }

  /* ── Help ── */
  .help-section { margin-bottom: 24px; }
  .help-section h2 { font-size: 18px; color: #fff; margin-bottom: 12px; }
  .help-section p { font-size: 14px; color: #aaa; line-height: 1.7; margin-bottom: 12px; }
  .help-section code { background: #1a1a1a; padding: 2px 8px; border-radius: 4px; font-size: 13px; color: #ccc; }
  .help-section pre {
    background: #141414; border: 1px solid #222; border-radius: 8px;
    padding: 16px; font-size: 13px; color: #aaa; line-height: 1.6;
    overflow-x: auto; margin: 12px 0;
  }

  .file-table { width: 100%; font-size: 13px; border-collapse: collapse; }
  .file-table td { padding: 8px 12px; border-bottom: 1px solid #1a1a1a; }
  .file-table td:first-child { color: #ccc; font-family: monospace; white-space: nowrap; width: 200px; }
  .file-table td:last-child { color: #888; }

  details { margin-bottom: 8px; }
  details summary {
    cursor: pointer; padding: 12px 16px; background: #141414; border: 1px solid #222;
    border-radius: 8px; font-size: 14px; color: #ccc; list-style: none;
  }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "+ "; color: #555; }
  details[open] summary::before { content: "- "; }
  details[open] summary { border-radius: 8px 8px 0 0; border-bottom: none; }
  details .faq-body {
    padding: 16px; background: #141414; border: 1px solid #222; border-top: none;
    border-radius: 0 0 8px 8px; font-size: 13px; color: #999; line-height: 1.6;
  }

  /* ── Toast ── */
  .toast {
    position: fixed; bottom: 24px; right: 24px; padding: 12px 20px;
    border-radius: 8px; font-size: 13px; z-index: 1000;
    animation: fadeIn 0.2s, fadeOut 0.3s 2s forwards;
  }
  .toast.success { background: #152a1a; color: #40c060; border: 1px solid #2d8a4e; }
  .toast.error { background: #2a1515; color: #e05050; border: 1px solid #8a2d2d; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; } }
  @keyframes fadeOut { from { opacity: 1; } to { opacity: 0; } }

  /* ── Tables ── */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 14px; border-bottom: 1px solid #222; color: #666; font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 12px 14px; border-bottom: 1px solid #1a1a1a; vertical-align: top; max-width: 300px; overflow: hidden; text-overflow: ellipsis; }
  tr:hover td { background: #151515; }

  .badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 500; }
  .badge-new { background: #1a2332; color: #4a9eff; }
  .badge-contacted { background: #2a2215; color: #f0a030; }
  .badge-replied { background: #152a1a; color: #40c060; }
  .badge-meeting { background: #2a1530; color: #c050e0; }
  .badge-draft { background: #1a1a2a; color: #8080ff; }
  .badge-active { background: #152a1a; color: #40c060; }
  .badge-open { background: #2a2215; color: #f0a030; }
  .badge-closed { background: #1a1a1a; color: #666; }
  .badge-lost { background: #2a1515; color: #e05050; }
  .badge-interested { background: #152a1a; color: #40c060; }
  .badge-objection { background: #2a2215; color: #f0a030; }
  .badge-not_interested { background: #2a1515; color: #e05050; }

  .campaign-card { background: #141414; border: 1px solid #222; border-radius: 10px; padding: 24px; margin-bottom: 20px; }
  .campaign-card h3 { font-size: 16px; color: #fff; margin-bottom: 4px; }
  .campaign-card .meta { font-size: 12px; color: #555; margin-bottom: 16px; display: flex; gap: 16px; align-items: center; }

  .email-step { border-left: 3px solid #222; padding: 16px 20px; margin-bottom: 12px; margin-left: 8px; }
  .email-step .step-num { font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .email-step .subject { font-size: 14px; font-weight: 600; color: #ddd; margin-bottom: 8px; }
  .email-step .body { font-size: 13px; color: #888; line-height: 1.7; white-space: pre-wrap; }

  .convo-card { background: #141414; border: 1px solid #222; border-radius: 10px; padding: 24px; margin-bottom: 16px; }
  .convo-card h3 { font-size: 15px; color: #fff; margin-bottom: 4px; }
  .convo-card .meta { font-size: 12px; color: #555; margin-bottom: 16px; display: flex; gap: 16px; align-items: center; }

  .thread-msg { padding: 12px 16px; margin-bottom: 8px; border-radius: 8px; max-width: 80%; font-size: 13px; line-height: 1.6; white-space: pre-wrap; }
  .thread-msg.sent { background: #1a2332; color: #b0d0ff; margin-left: auto; }
  .thread-msg.received { background: #1a1a1a; color: #ccc; }
  .thread-msg .sender { font-size: 11px; color: #666; margin-bottom: 4px; }

  .activity-item { display: flex; gap: 16px; padding: 12px 0; border-bottom: 1px solid #1a1a1a; font-size: 13px; align-items: center; }
  .activity-item .time { color: #444; font-size: 12px; min-width: 140px; font-variant-numeric: tabular-nums; }
  .activity-item .agent { color: #666; min-width: 80px; }
  .activity-item .action { color: #aaa; }

  .empty { text-align: center; padding: 60px 20px; color: #444; font-size: 14px; }
  .empty .big { font-size: 40px; margin-bottom: 16px; }
</style>
</head>
<body>

<header>
  <h1>Harvey <span>Always Be Closing</span></h1>
  <div class="header-controls">
    <div class="harvey-status" id="header-status">
      <div class="status-dot stopped" id="header-dot"></div>
      <span id="header-status-text">Checking...</span>
    </div>
    <button class="refresh-btn" onclick="loadCurrentTab()">Refresh</button>
  </div>
</header>

<nav>
  <button class="active" onclick="showTab('setup', this)">Setup</button>
  <button onclick="showTab('overview', this)">Overview</button>
  <button onclick="showTab('companies', this)">Companies</button>
  <button onclick="showTab('prospects', this)">Contacts</button>
  <button onclick="showTab('campaigns', this)">Campaigns</button>
  <button onclick="showTab('conversations', this)">Conversations</button>
  <button onclick="showTab('activity', this)">Activity</button>
  <button onclick="showTab('settings', this)">Settings</button>
  <button onclick="showTab('controls', this)">Controls</button>
  <button onclick="showTab('help', this)">Help</button>
</nav>

<main>

<!-- ── Setup ── -->
<div id="setup" class="section active">
  <div class="card">
    <h2>Setup Progress</h2>
    <div class="progress-wrap" id="setup-progress"></div>
    <div id="setup-checklist"></div>
  </div>
</div>

<!-- ── Overview ── -->
<div id="overview" class="section">
  <div class="stats-grid" id="stats-grid"></div>
</div>

<!-- ── Companies ── -->
<div id="companies" class="section">
  <div id="companies-list"></div>
</div>

<!-- ── Contacts ── -->
<div id="prospects" class="section">
  <div id="prospects-table"></div>
</div>

<!-- ── Campaigns ── -->
<div id="campaigns" class="section">
  <div id="campaigns-list"></div>
</div>

<!-- ── Conversations ── -->
<div id="conversations" class="section">
  <div id="conversations-list"></div>
</div>

<!-- ── Activity ── -->
<div id="activity" class="section">
  <div id="activity-list"></div>
</div>

<!-- ── Settings ── -->
<div id="settings" class="section">
  <div class="card">
    <h2>Instantly (Email Platform)</h2>
    <p style="font-size:13px;color:#888;margin-bottom:16px">Required. Get your API key from <a href="https://app.instantly.ai/app/settings/integrations" target="_blank" style="color:#4a9eff">Instantly Settings &gt; Integrations</a>.</p>
    <div class="form-group">
      <label class="form-label">API Key</label>
      <div class="form-row">
        <div class="form-group" style="margin-bottom:0">
          <input type="password" class="form-input" id="instantly-key" placeholder="Enter your Instantly API key">
        </div>
        <button class="btn btn-secondary btn-sm" onclick="toggleVisibility('instantly-key')">Show</button>
        <button class="btn btn-secondary btn-sm" onclick="testInstantly()">Test</button>
      </div>
      <div id="instantly-test-result"></div>
    </div>
    <button class="btn btn-primary" onclick="saveInstantly()">Save</button>
  </div>

  <div class="card">
    <h2>LinkedIn <span class="optional-tag">optional</span></h2>
    <p style="font-size:13px;color:#888;margin-bottom:16px">For automated LinkedIn prospecting. Harvey logs in and searches like a human.</p>
    <div class="form-group">
      <label class="form-label">Email / Username</label>
      <input type="text" class="form-input" id="linkedin-email" placeholder="your@email.com">
    </div>
    <div class="form-group">
      <label class="form-label">Password</label>
      <input type="password" class="form-input" id="linkedin-password" placeholder="Enter password">
    </div>
    <button class="btn btn-primary" onclick="saveLinkedIn()">Save</button>
  </div>

  <div class="card">
    <h2>Cloudflare <span class="optional-tag">optional</span></h2>
    <p style="font-size:13px;color:#888;margin-bottom:16px">For deep website crawling with JavaScript rendering during product training. ~$5/month.</p>
    <div class="form-group">
      <label class="form-label">Account ID</label>
      <input type="text" class="form-input" id="cf-account-id" placeholder="Your Cloudflare Account ID">
    </div>
    <div class="form-group">
      <label class="form-label">API Token</label>
      <input type="password" class="form-input" id="cf-api-token" placeholder="Your Cloudflare API Token">
    </div>
    <button class="btn btn-primary" onclick="saveCloudflare()">Save</button>
  </div>
</div>

<!-- ── Controls ── -->
<div id="controls" class="section">
  <div class="control-panel">
    <div class="card">
      <h2>Harvey Controls</h2>
      <div class="status-big" id="control-status">
        <div class="dot stopped" id="control-dot"></div>
        <span class="label stopped" id="control-label">Stopped</span>
      </div>
      <div class="status-meta" id="control-meta"></div>
      <div class="btn-group">
        <button class="btn btn-primary" id="btn-start" onclick="startHarvey()">Start Harvey</button>
        <button class="btn btn-danger" id="btn-stop" onclick="stopHarvey()" style="display:none">Stop Harvey</button>
      </div>
    </div>
    <div class="card">
      <h2>Recent Logs</h2>
      <div class="log-viewer" id="log-viewer">No logs yet. Start Harvey to see activity.</div>
      <div class="btn-group">
        <button class="btn btn-secondary btn-sm" onclick="loadLogs()">Refresh Logs</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Help ── -->
<div id="help" class="section">

  <div class="help-section">
    <h2>What is Harvey?</h2>
    <p>Harvey is an autonomous AI sales agent. Once set up, Harvey runs on its own: finds people who match your ideal customer, writes personalized cold emails, sends them through your email platform, reads every reply, handles objections, and works toward booking a meeting. You review everything through this dashboard.</p>
    <p>Harvey runs on your Claude Max subscription, so there are no extra AI costs. Everything stays on your machine in one folder.</p>
  </div>

  <div class="help-section">
    <h2>Getting Started</h2>
    <p>There are three things to do:</p>
    <pre>1. Go to the Settings tab and enter your Instantly API key
2. Train Harvey on your product (through Claude or the command line)
3. Go to the Controls tab and click Start</pre>
    <p>The Setup tab shows you exactly what's done and what still needs to happen.</p>
  </div>

  <div class="help-section">
    <h2>Where Everything Lives</h2>
    <p>Everything Harvey needs is inside this one project folder. Nothing is stored elsewhere.</p>
    <table class="file-table">
      <tr><td>.env</td><td>Your API keys and credentials (never shared or committed)</td></tr>
      <tr><td>harvey.yaml</td><td>Your product info, target customers, and behavior settings</td></tr>
      <tr><td>skills/</td><td>Sales knowledge files. Edit these to change how Harvey writes and sells.</td></tr>
      <tr><td>skills/product_knowledge.md</td><td>Everything Harvey knows about your product (auto-generated from training)</td></tr>
      <tr><td>prompts/</td><td>Prompt templates for each agent. Advanced customization.</td></tr>
      <tr><td>data/harvey.db</td><td>Database with all prospects, campaigns, and conversations</td></tr>
      <tr><td>data/harvey.log</td><td>Log file showing what Harvey is doing</td></tr>
    </table>
  </div>

  <div class="help-section">
    <h2>Getting Your API Keys</h2>

    <details>
      <summary>Instantly API Key (required)</summary>
      <div class="faq-body">
        <p>Instantly is the email platform Harvey uses to send campaigns.</p>
        <p>1. Sign up at <a href="https://instantly.ai" target="_blank" style="color:#4a9eff">instantly.ai</a> (you need the Growth plan for API access)</p>
        <p>2. Go to Settings > Integrations</p>
        <p>3. Copy your API key</p>
        <p>4. Paste it in the Settings tab here</p>
      </div>
    </details>

    <details>
      <summary>LinkedIn Credentials (optional)</summary>
      <div class="faq-body">
        <p>If you want Harvey to find prospects on LinkedIn, enter your LinkedIn email and password. Harvey uses a real browser to search LinkedIn like a human would, with random delays and rate limits to avoid detection.</p>
        <p>If you skip this, Harvey will find prospects through Google searches and company website scraping instead.</p>
      </div>
    </details>

    <details>
      <summary>Cloudflare Browser Rendering (optional)</summary>
      <div class="faq-body">
        <p>This is only used during product training (when Harvey crawls your website to learn about your product). It handles JavaScript-heavy websites that a basic crawler can't read.</p>
        <p>1. Sign up at <a href="https://dash.cloudflare.com" target="_blank" style="color:#4a9eff">Cloudflare</a> (paid Workers plan, ~$5/month)</p>
        <p>2. Go to Workers & Pages > Browser Rendering</p>
        <p>3. Create an API token with Browser Rendering Edit permissions</p>
        <p>4. Enter your Account ID and API Token in the Settings tab</p>
        <p>Without this, Harvey uses a built-in crawler that works fine for most websites but can't render JavaScript.</p>
      </div>
    </details>
  </div>

  <div class="help-section">
    <h2>Common Issues</h2>

    <details>
      <summary>"command not found: harvey"</summary>
      <div class="faq-body">You need to activate the virtual environment first: <code>source .venv/bin/activate</code></div>
    </details>

    <details>
      <summary>Instantly API returns 401</summary>
      <div class="faq-body">Your API key is wrong, or you need the Growth plan (the free plan doesn't include API access). Double-check the key in Settings > Integrations in your Instantly dashboard.</div>
    </details>

    <details>
      <summary>Claude headless mode fails</summary>
      <div class="faq-body">Make sure you've run <code>claude login</code> in your terminal and have an active Claude Max subscription. Harvey uses your existing subscription, not a separate API key.</div>
    </details>

    <details>
      <summary>Harvey isn't finding prospects</summary>
      <div class="faq-body">Check that your ICP (ideal customer profile) in harvey.yaml has realistic titles, industries, and geography. If LinkedIn is set up, check that the credentials are correct. Check the Activity tab to see what Harvey has been trying to do.</div>
    </details>
  </div>
</div>

</main>

<script>
let currentTab = 'setup';

function showTab(id, btn) {
  currentTab = id;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (btn) btn.classList.add('active');
  loadCurrentTab();
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'setup': loadSetupStatus(); break;
    case 'overview': loadStats(); break;
    case 'companies': loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'settings': loadSettings(); break;
    case 'controls': loadHarveyStatus(); loadLogs(); break;
  }
}

function badge(status) {
  const cls = 'badge-' + (status || '').replace(/\\s+/g, '_');
  return `<span class="badge ${cls}">${status || 'unknown'}</span>`;
}

function formatDate(d) {
  if (!d) return '';
  try {
    const dt = new Date(d);
    return dt.toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return d; }
}

function escHtml(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2500);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Setup ──

async function loadSetupStatus() {
  const r = await fetch('/api/setup-status');
  const data = await r.json();
  const pct = data.percent || 0;
  const color = pct === 100 ? 'green' : 'yellow';

  document.getElementById('setup-progress').innerHTML = `
    <div class="progress-label">
      <span class="pct">${pct}% complete</span>
      <span class="text">${data.completed}/${data.total_required} required steps done</span>
    </div>
    <div class="progress-bar"><div class="progress-fill ${color}" style="width:${pct}%"></div></div>`;

  const required = data.checks.filter(c => c.required);
  const optional = data.checks.filter(c => !c.required);

  let html = '';
  for (const c of required) {
    const icon = c.done ? '<span class="check-icon done">&#10003;</span>' : '<span class="check-icon pending">&#9675;</span>';
    html += `<div class="check-item">
      ${icon}
      <div class="check-info">
        <div class="check-label ${c.done ? 'done' : ''}">${escHtml(c.label)}</div>
        ${!c.done ? `<div class="check-help">${escHtml(c.help)}</div>` : ''}
      </div>
    </div>`;
  }

  if (optional.length) {
    html += '<div style="margin-top:16px;margin-bottom:8px;font-size:12px;color:#555;text-transform:uppercase;letter-spacing:0.5px">Optional</div>';
    for (const c of optional) {
      const icon = c.done ? '<span class="check-icon done">&#10003;</span>' : '<span class="check-icon pending">&#9675;</span>';
      html += `<div class="check-item">
        ${icon}
        <div class="check-info">
          <div class="check-label ${c.done ? 'done' : ''}">${escHtml(c.label)} <span class="optional-tag">optional</span></div>
          ${!c.done ? `<div class="check-help">${escHtml(c.help)}</div>` : ''}
        </div>
      </div>`;
    }
  }

  document.getElementById('setup-checklist').innerHTML = html;
}

// ── Settings ──

async function loadSettings() {
  const r = await fetch('/api/settings');
  const data = await r.json();
  document.getElementById('instantly-key').value = data.instantly_api_key || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('linkedin-password').value = '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';
  document.getElementById('cf-api-token').value = '';
  if (data.linkedin_password_set) {
    document.getElementById('linkedin-password').placeholder = 'Password saved (enter new to change)';
  }
}

async function saveInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  await fetch('/api/settings/env', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({INSTANTLY_API_KEY: key})
  });
  showToast('Instantly API key saved.', 'success');
}

async function saveLinkedIn() {
  const email = document.getElementById('linkedin-email').value.trim();
  const pass = document.getElementById('linkedin-password').value;
  const data = {LINKEDIN_EMAIL: email};
  if (pass) data.LINKEDIN_PASSWORD = pass;
  await fetch('/api/settings/env', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(data)
  });
  showToast('LinkedIn credentials saved.', 'success');
}

async function saveCloudflare() {
  const id = document.getElementById('cf-account-id').value.trim();
  const token = document.getElementById('cf-api-token').value.trim();
  await fetch('/api/settings/env', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({CLOUDFLARE_ACCOUNT_ID: id, CLOUDFLARE_API_TOKEN: token})
  });
  showToast('Cloudflare credentials saved.', 'success');
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result" style="background:#1a1a1a;color:#888">Testing...</div>';
  const r = await fetch('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({api_key: key})
  });
  const data = await r.json();
  el.innerHTML = `<div class="test-result ${data.success ? 'success' : 'error'}">${escHtml(data.message)}</div>`;
}

// ── Controls ──

async function loadHarveyStatus() {
  const r = await fetch('/api/harvey/status');
  const data = await r.json();
  const running = data.running;

  document.getElementById('header-dot').className = 'status-dot ' + (running ? 'running' : 'stopped');
  document.getElementById('header-status-text').textContent = running ? 'Running' : 'Stopped';
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  document.getElementById('control-label').className = 'label ' + (running ? 'running' : 'stopped');
  document.getElementById('control-label').textContent = running ? 'Running' : 'Stopped';

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = `PID: ${data.pid}`;
    if (data.started_at) info += ` &middot; Started: ${formatDate(data.started_at)}`;
    meta.innerHTML = info;
  } else {
    meta.innerHTML = '';
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startHarvey() {
  document.getElementById('btn-start').disabled = true;
  const r = await fetch('/api/harvey/start', {method: 'POST'});
  const data = await r.json();
  if (data.success) {
    showToast('Harvey started.', 'success');
  } else {
    showToast(data.message || 'Failed to start.', 'error');
  }
  document.getElementById('btn-start').disabled = false;
  loadHarveyStatus();
}

async function stopHarvey() {
  document.getElementById('btn-stop').disabled = true;
  const r = await fetch('/api/harvey/stop', {method: 'POST'});
  const data = await r.json();
  if (data.success) {
    showToast('Harvey stopped.', 'success');
  } else {
    showToast(data.message || 'Failed to stop.', 'error');
  }
  document.getElementById('btn-stop').disabled = false;
  loadHarveyStatus();
}

async function loadLogs() {
  const r = await fetch('/api/harvey/logs');
  const data = await r.json();
  const el = document.getElementById('log-viewer');
  if (data.lines && data.lines.length) {
    el.textContent = data.lines.join('\\n');
    el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = 'No logs yet. Start Harvey to see activity.';
  }
}

// ── Pipeline Data ──

async function loadStats() {
  const r = await fetch('/api/stats');
  const data = await r.json();
  if (data.error) {
    document.getElementById('stats-grid').innerHTML = `<div class="empty"><div class="big">No data yet</div>Start Harvey to begin building your pipeline.</div>`;
    return;
  }
  const p = data.prospects || {}, c = data.campaigns || {}, v = data.conversations || {};
  const bd = (map) => Object.entries(map).map(([k,v]) => `${k}: ${v}`).join(' &middot; ') || 'none';
  document.getElementById('stats-grid').innerHTML = `
    <div class="stat-card"><div class="label">Prospects</div><div class="value">${p.total||0}</div><div class="breakdown">${bd(p.by_status||{})}</div></div>
    <div class="stat-card"><div class="label">Campaigns</div><div class="value">${c.total||0}</div><div class="breakdown">${bd(c.by_status||{})}</div></div>
    <div class="stat-card"><div class="label">Conversations</div><div class="value">${v.total||0}</div><div class="breakdown">${bd(v.by_status||{})}</div></div>
    <div class="stat-card"><div class="label">Actions Today</div><div class="value">${data.actions_total||0}</div><div class="breakdown">Claude calls today: ${data.claude_calls_today||0}</div></div>`;
}

async function loadCompanies() {
  const r = await fetch('/api/companies');
  const data = await r.json();
  if (!data.length) {
    document.getElementById('companies-list').innerHTML = `<div class="empty"><div class="big">No companies yet</div>Harvey hasn't researched any companies yet.</div>`;
    return;
  }
  let html = `<table><thead><tr><th>Company</th><th>Domain</th><th>Industry</th><th>Size</th><th>Location</th><th>Contacts</th><th>Source</th><th>Added</th></tr></thead><tbody>`;
  for (const c of data) {
    const website = c.website || (c.domain ? 'https://'+c.domain : '');
    const nameLink = website ? `<a href="${escHtml(website)}" target="_blank" style="color:#4a9eff">${escHtml(c.name)}</a>` : escHtml(c.name);
    html += `<tr style="cursor:pointer" onclick="showCompanyContacts('${c.id}', '${escHtml(c.name)}')">
      <td>${nameLink}</td><td>${escHtml(c.domain)}</td><td>${escHtml(c.industry)}</td><td>${escHtml(c.company_size)}</td><td>${escHtml(c.location)}</td><td>${c.contact_count||0}</td><td>${escHtml(c.source)}</td><td>${formatDate(c.created_at)}</td></tr>`;
  }
  document.getElementById('companies-list').innerHTML = html + '</tbody></table>';
}

async function showCompanyContacts(companyId, companyName) {
  const r = await fetch('/api/companies/' + companyId + '/contacts');
  const data = await r.json();
  let html = `<div class="card" style="margin-bottom:16px"><h2>${escHtml(companyName)} - Contacts</h2><button class="btn btn-secondary btn-sm" onclick="loadCompanies()" style="margin-bottom:16px">Back to Companies</button>`;
  if (!data.length) {
    html += '<p style="color:#888">No contacts found at this company.</p></div>';
  } else {
    html += '<table><thead><tr><th>Name</th><th>Title</th><th>Email</th><th>Phone</th><th>LinkedIn</th><th>Status</th><th>Source</th></tr></thead><tbody>';
    for (const p of data) {
      const emailIcon = p.email_verified ? ' &#10003;' : '';
      const phoneIcon = p.phone_verified ? ' &#10003;' : '';
      html += `<tr><td>${escHtml(p.first_name)} ${escHtml(p.last_name)}</td><td>${escHtml(p.title)}</td><td>${escHtml(p.email)}${emailIcon}</td><td>${escHtml(p.phone)}${phoneIcon}</td><td>${p.linkedin_url ? '<a href="'+escHtml(p.linkedin_url)+'" target="_blank" style="color:#4a9eff">Profile</a>' : ''}</td><td>${badge(p.status)}</td><td>${escHtml(p.source)}</td></tr>`;
    }
    html += '</tbody></table></div>';
  }
  document.getElementById('companies-list').innerHTML = html;
}

async function addFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || 'Add your feedback:');
  if (!comment) return;
  await fetch('/api/feedback', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  showToast('Feedback saved.', 'success');
}

async function loadProspects() {
  const r = await fetch('/api/prospects');
  const data = await r.json();
  if (!data.length) {
    document.getElementById('prospects-table').innerHTML = `<div class="empty"><div class="big">No prospects yet</div>Harvey hasn't found any prospects yet.</div>`;
    return;
  }
  let html = `<table><thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th><th>Status</th><th>Source</th><th>Added</th><th></th></tr></thead><tbody>`;
  for (const p of data) {
    const emailV = p.email ? (escHtml(p.email) + (p.email_verified ? ' <span style="color:#40c060">&#10003;</span>' : '')) : '';
    const phoneV = p.phone ? (escHtml(p.phone) + (p.phone_verified ? ' <span style="color:#40c060">&#10003;</span>' : '')) : '';
    html += `<tr><td>${escHtml(p.first_name)} ${escHtml(p.last_name)}</td><td>${escHtml(p.title)}</td><td>${escHtml(p.company)}</td><td>${emailV}</td><td>${phoneV}</td><td>${badge(p.status)}</td><td>${escHtml(p.source)}</td><td>${formatDate(p.created_at)}</td><td><button class="btn btn-secondary btn-sm" onclick="addFeedback('contact','${p.id}','Feedback on this contact:')">Feedback</button></td></tr>`;
  }
  document.getElementById('prospects-table').innerHTML = html + '</tbody></table>';
}

async function loadCampaigns() {
  const r = await fetch('/api/campaigns');
  const data = await r.json();
  if (!data.length) {
    document.getElementById('campaigns-list').innerHTML = `<div class="empty"><div class="big">No campaigns yet</div>Harvey hasn't written any email sequences yet.</div>`;
    return;
  }
  let html = '';
  for (const c of data) {
    let stepsHtml = '';
    for (const step of (c.sequence || [])) {
      stepsHtml += `<div class="email-step"><div class="step-num">Email ${step.step||'?'}${step.delay_days ? ` &middot; Send after ${step.delay_days} days`:''}</div><div class="subject">${escHtml(step.subject)}</div><div class="body">${escHtml(step.body)}</div></div>`;
    }
    const pc = (c.prospect_ids||[]).length;
    html += `<div class="campaign-card"><h3>${escHtml(c.name||'Untitled Campaign')}</h3><div class="meta">${badge(c.status)}<span>${c.channel||'email'}</span><span>${pc} prospect${pc!==1?'s':''}</span><span>${formatDate(c.created_at)}</span><button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();addFeedback('campaign','${c.id}','Leave feedback on this campaign:')">Feedback</button></div>${stepsHtml||'<div class="empty">No email steps</div>'}</div>`;
  }
  document.getElementById('campaigns-list').innerHTML = html;
}

async function loadConversations() {
  const r = await fetch('/api/conversations');
  const data = await r.json();
  if (!data.length) {
    document.getElementById('conversations-list').innerHTML = `<div class="empty"><div class="big">No conversations yet</div>Harvey hasn't received any replies yet.</div>`;
    return;
  }
  let html = '';
  for (const c of data) {
    let threadHtml = '';
    for (const msg of (c.thread || [])) {
      const cls = msg.sender === 'harvey' ? 'sent' : 'received';
      threadHtml += `<div class="thread-msg ${cls}"><div class="sender">${escHtml(msg.sender)} &middot; ${formatDate(msg.timestamp)}</div>${escHtml(msg.content)}</div>`;
    }
    const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || 'Unknown';
    html += `<div class="convo-card"><h3>${escHtml(name)} &mdash; ${escHtml(c.company||'')}</h3><div class="meta">${badge(c.status)}${c.intent?badge(c.intent):''}<span>${escHtml(c.prospect_email||'')}</span><span>${formatDate(c.updated_at)}</span></div>${threadHtml||'<div class="empty">No messages</div>'}</div>`;
  }
  document.getElementById('conversations-list').innerHTML = html;
}

async function loadActivity() {
  const r = await fetch('/api/activity');
  const data = await r.json();
  if (!data.length) {
    document.getElementById('activity-list').innerHTML = `<div class="empty"><div class="big">No activity yet</div>Harvey hasn't taken any actions yet.</div>`;
    return;
  }
  let html = '';
  for (const a of data) {
    html += `<div class="activity-item"><span class="time">${formatDate(a.created_at)}</span><span class="agent">${escHtml(a.agent)}</span><span class="action">${escHtml(a.action_type)}</span></div>`;
  }
  document.getElementById('activity-list').innerHTML = html;
}

// ── Init ──
loadSetupStatus();
loadHarveyStatus();
setInterval(loadHarveyStatus, 10000);
</script>
</body>
</html>"""


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server."""
    import uvicorn

    print(f"\\n  Harvey Dashboard running at http://{host}:{port}")
    print(f"  Press Ctrl+C to stop.\\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
