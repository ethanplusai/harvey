# Harvey — Autonomous AI Sales Agent

Harvey is an autonomous sales agent that finds prospects, writes personalized cold email campaigns, sends them via Instantly, monitors replies, handles objections, and books meetings — all without human intervention.

## First-Time Setup

When a user opens this project in Claude Code for the first time, help them get Harvey configured and running. Check the current state to determine what's needed:

### 1. Check what's already done

- **Python venv**: Check if `.venv/` exists. If not, create one: `python3 -m venv .venv && source .venv/bin/activate && pip install -e .`
- **Dependencies**: Check if `harvey` package is importable. If not: `source .venv/bin/activate && pip install -e .`
- **Playwright**: Check if chromium is installed. If not: `python -m playwright install chromium`
- **`.env` file**: Check if `.env` exists and has real values (not placeholders). If not, walk the user through creating it.
- **`harvey.yaml`**: Check if it exists and has real values (not "Your Company" / "Your Product" placeholders). If not, walk the user through product training.

### 2. Environment Variables (`.env`)

Ask the user for each of these. Only the Instantly API key is required — the rest are optional:

```
INSTANTLY_API_KEY=          # Required — from Instantly Settings → Integrations
LINKEDIN_EMAIL=             # Optional — for LinkedIn prospecting
LINKEDIN_PASSWORD=          # Optional — for LinkedIn prospecting
CLOUDFLARE_ACCOUNT_ID=      # Optional — for deep JS-rendered website crawling
CLOUDFLARE_API_TOKEN=       # Optional — for deep JS-rendered website crawling
HUNTER_API_KEY=             # Optional — for email verification fallback
```

After getting the Instantly API key, test it:
```bash
source .venv/bin/activate && python3 -c "
import asyncio, httpx
async def test():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get('https://api.instantly.ai/api/v2/accounts', headers={'Authorization': 'Bearer API_KEY_HERE'})
        print('Connected!' if r.status_code == 200 else f'Failed: {r.status_code}')
asyncio.run(test())
"
```

### 3. Product Training

This is the most important step. Harvey needs to know what it's selling. There are two approaches:

**Option A — Train from a website URL (recommended):**
Run the trainer which crawls the site and generates config + product knowledge automatically:
```bash
source .venv/bin/activate && python -m harvey.trainer https://their-website.com
```
This generates: `harvey.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`

**Option B — Manual configuration:**
Ask the user these questions and build `harvey.yaml` and `skills/product_knowledge.md` from their answers:
- Company name
- Product/service name and description
- Pricing
- Key benefits (3-5)
- Target industries
- Target job titles (decision-makers)
- Target company size
- Target geography
- Persona name and email (the "from" identity for outreach)
- Common objections they hear and how to respond

### 4. Behavior Settings

These go in `harvey.yaml` under `usage:`. Ask the user or use sensible defaults:
- `max_daily_claude_percent`: How much of their Claude daily quota Harvey can use (default: 80)
- `heartbeat_interval_minutes`: How often Harvey checks for work (default: 15)
- `quiet_hours.start` / `quiet_hours.end`: When Harvey sleeps (default: 22:00-07:00)
- `quiet_hours.timezone`: User's timezone (default: America/New_York)
- Max daily email sends (default: 50)

### 5. Start Harvey

Once configured:
```bash
source .venv/bin/activate && python -m harvey
```

## Project Structure

```
harvey/
├── harvey.yaml              # Config — persona, product, ICP, settings
├── .env                     # API keys (never committed)
├── harvey/
│   ├── main.py              # Heartbeat loop — the core engine
│   ├── brain.py             # Claude Code headless wrapper + skills loader
│   ├── state.py             # SQLite state manager
│   ├── config.py            # Configuration loader + validation
│   ├── cli.py               # CLI commands (harvey setup/run/train/status)
│   ├── setup.py             # Interactive setup wizard
│   ├── trainer.py           # Auto-train from a website URL
│   ├── agents/
│   │   ├── scout.py         # DIY prospecting (LinkedIn, Google, company sites)
│   │   ├── writer.py        # Email sequence generation
│   │   ├── sender.py        # Campaign deployment via Instantly
│   │   └── handler.py       # Reply processing + auto-response
│   ├── integrations/
│   │   ├── instantly.py     # Instantly API v2 client
│   │   ├── linkedin.py      # Playwright browser automation
│   │   └── email_finder.py  # Email pattern discovery + SMTP verification
│   └── models/
│       ├── prospect.py      # Prospect data model
│       ├── campaign.py      # Campaign + email sequence models
│       └── conversation.py  # Conversation thread model
├── prompts/                 # Prompt templates for each agent
├── skills/                  # Sales knowledge files (editable markdown)
└── data/                    # Runtime data (auto-created)
    └── harvey.db            # SQLite database
```

## How Harvey Works

Harvey runs a heartbeat loop (`main.py`). Every cycle it:
1. Checks quiet hours → sleeps if needed
2. Checks Claude usage budget → stops if over limit
3. Asks Claude what to do next based on pipeline state
4. Executes the chosen action via the appropriate sub-agent
5. Logs the action and sleeps until next cycle

Priority order: handle replies > send campaigns > write campaigns > prospect > idle

## Sub-Agents

- **Scout** (`agents/scout.py`): Finds prospects via LinkedIn search, Google dorking, and company website scraping. Discovers emails via SMTP pattern verification.
- **Writer** (`agents/writer.py`): Writes personalized 3-email sequences using copywriting frameworks (AIDA, PAS, BAB).
- **Sender** (`agents/sender.py`): Deploys campaigns to Instantly — creates campaign, adds leads, sets sequence, activates.
- **Handler** (`agents/handler.py`): Polls for replies, classifies intent (interested/objection/not interested/OOO/wrong person), auto-responds.

## Key Commands

```bash
source .venv/bin/activate    # Always activate venv first
harvey run                   # Start the heartbeat loop
harvey dashboard             # Open the web dashboard at http://localhost:5555
harvey setup                 # Re-run the setup wizard
harvey train <url>           # Train on a new product website
harvey train <url> 500       # Train with more pages
harvey status                # Show pipeline summary
python -m harvey             # Alternative to 'harvey run'
```

## Common Issues

- **"command not found: harvey"**: Need to activate venv first: `source .venv/bin/activate`
- **"externally-managed-environment"**: Need to use a venv, not system Python
- **SQLite "unable to open database file"**: The `data/` directory is created automatically on first run
- **Claude headless mode fails**: User needs to run `claude login` and have an active Max subscription
- **Instantly API 401**: API key is wrong or user needs the Growth plan for API access
