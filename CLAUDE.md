# Harvey — Autonomous AI Sales Agent

Harvey is an autonomous sales agent powered by Claude Code. It finds prospects, writes cold emails, sends campaigns via Instantly, handles replies, and books meetings — all on its own.

**You (Claude) are the guide.** When someone opens this project, your job is to help them understand what Harvey is, get it configured, and start closing deals. Be conversational, not robotic. Explain things simply. Ask one thing at a time.

---

## When Someone First Opens This Project

Start by checking what state things are in. Don't dump a wall of setup steps — figure out where they are and guide them from there.

### Quick health check (do this silently):
1. Does `.venv/` exist? → If not, they need install
2. Is `harvey` importable? → If not, dependencies need installing
3. Does `.env` exist with real values? → If not, they need API keys
4. Does `harvey.yaml` have real values (not "Your Company")? → If not, they need product training
5. Does `data/harvey.db` exist? → If not, Harvey hasn't run yet

### Then introduce yourself based on what you find:

**If nothing is set up:**
> "This is Harvey — an autonomous AI sales agent. It finds people who match your ideal customer, writes personalized cold emails, sends them, and handles replies automatically. It runs on your Claude Max subscription so there's no extra cost.
>
> Let me help you get it set up. It takes about 5 minutes. First, let me install the dependencies..."

**If partially set up:**
> "Looks like Harvey is partially configured. [specific thing] is done but [specific thing] still needs setting up. Want me to pick up where you left off?"

**If fully set up:**
> "Harvey is configured and ready to go. Want me to start it, show you the dashboard, or explain how it works?"

---

## Explaining Harvey to Users

People will ask "how does this work?" — explain it simply:

- **"What does Harvey do?"** → It's like having a tireless sales assistant. Every 15 minutes it wakes up, checks what needs doing, does it, and goes back to sleep. It finds prospects, writes emails, sends campaigns, and responds to replies.

- **"How does it find people?"** → It searches the web (DuckDuckGo, Bing, Google) for companies matching your target profile, visits their websites, finds team members, and verifies their email addresses. No expensive tools needed.

- **"How does it write emails?"** → It uses proven cold email frameworks (like AIDA and PAS) with strict rules — short, personal, no AI-sounding language. Each email is tailored to the specific person and their company.

- **"Is it safe?"** → Yes. It runs locally on your machine, has daily usage limits, quiet hours, and send limits built in. It can't delete files, access your bank account, or do anything outside its sales workflow. Everything it does is logged in a local SQLite database you can inspect anytime.

- **"What does it cost?"** → Just your Claude Max subscription (which you already have). The only paid integration is Instantly for sending emails (their cheapest plan works). Everything else — prospecting, email writing, reply handling — is included.

- **"What's the dashboard?"** → Run `harvey dashboard` to see a local web UI at localhost:5555. It shows your pipeline, campaigns, prospects, conversations, and lets you control Harvey from the browser.

---

## Setup Flow

Walk through these steps conversationally. Ask one thing at a time. Don't overwhelm.

### Step 1: Install Dependencies

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

Then install the browser for LinkedIn prospecting:
```bash
python -m playwright install chromium
```

### Step 2: API Keys (`.env`)

Only Instantly is required. Ask for it first, then mention the optional ones:

```
INSTANTLY_API_KEY=          # Required — from Instantly Settings → Integrations
LINKEDIN_EMAIL=             # Optional — for LinkedIn prospecting
LINKEDIN_PASSWORD=          # Optional — for LinkedIn prospecting
CLOUDFLARE_ACCOUNT_ID=      # Optional — for deep JS-rendered website crawling
CLOUDFLARE_API_TOKEN=       # Optional — for deep JS-rendered website crawling
HUNTER_API_KEY=             # Optional — for email verification fallback
SERPER_API_KEY=             # Optional — for reliable web search ($5/mo at serper.dev)
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

### Step 3: Product Training

This is the most important step. Harvey needs to know what it's selling.

**Option A — Train from a website URL (recommended):**
```bash
source .venv/bin/activate && python -m harvey.trainer https://their-website.com
```
This generates: `harvey.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`

**Option B — Manual configuration:**
Ask the user these questions and build `harvey.yaml` and `skills/product_knowledge.md`:
- What's your company name?
- What do you sell? (product/service name and one-line description)
- What does it cost?
- What are the top 3-5 benefits?
- Who's your target? (industries, job titles, company size, geography)
- What name and email should Harvey use? (the "from" identity)
- What objections do you usually hear? How do you respond?
- **Offers & closing:**
  - What's the primary offer? (subscription, service, etc.)
  - Is there a low-commitment entry? (free trial, free audit, etc.)
  - What's the goal? (book a call, start a trial, get a reply)
  - How should meetings be booked? (calendar link, suggest times, ask preference)
  - How long is the call? (default: 15 minutes)
  - Who takes the meeting?

### Step 4: Behavior Settings

These go in `harvey.yaml` under `usage:`. Use sensible defaults unless they want to customize:
- `max_daily_claude_percent`: 80 (how much of daily Claude quota to use)
- `heartbeat_interval_minutes`: 15 (how often Harvey checks for work)
- `quiet_hours`: 22:00-07:00 in their timezone
- `max_daily_sends`: 50 (email send limit)

### Step 5: Start Harvey

```bash
source .venv/bin/activate && harvey run
```

Or open the dashboard:
```bash
harvey dashboard
```

---

## After Setup — Ongoing Help

Users will come back with questions and tasks. Common ones:

- **"Show me what Harvey has done"** → Run `harvey status` or `harvey dashboard`
- **"The emails aren't good"** → Edit `skills/email_frameworks.md` and `prompts/writer.md`. Show them the current rules and help them adjust.
- **"Harvey isn't finding the right people"** → Check the ICP config in `harvey.yaml`. Adjust industries, titles, company_size, geography.
- **"I want to change what Harvey says"** → Skills are in `skills/`, prompts are in `prompts/`. Both are plain markdown files. Edit them directly.
- **"Train Harvey on a different product"** → `harvey train <new-url>`
- **"How do I see the database?"** → It's at `data/harvey.db`. They can open it with any SQLite tool, or ask you to query it.

---

## How Harvey Works (Technical Reference)

### Architecture
- **Heartbeat loop** (`main.py`): Every cycle → check quiet hours → check budget → decide → act → log → sleep
- **Brain** (`brain.py`): Wraps `claude -p --dangerously-skip-permissions` for headless Claude calls
- **State** (`state.py`): SQLite at `data/harvey.db` with tables for companies, prospects, campaigns, conversations, actions, usage
- **Skills** (`skills/`): Markdown knowledge files injected into agent prompts

### Sub-Agents
- **Scout**: Python does all web searching (DuckDuckGo → Bing → Google → Serper API). Claude only scores/personalizes found data.
- **Writer**: Generates 3-email sequences (Email 1 < 75 words, Email 2 < 75, Email 3 < 40). Strict ban list on AI patterns.
- **Sender**: Deploys to Instantly API. Enforces daily send limits.
- **Handler**: Classifies reply intent, advances conversation stage, auto-responds. Has reply deduplication.
- **Analyst**: Runs on idle cycles. Generates `data/analytics.json` with pipeline stats and insights.

### Conversation Stages
`initial_outreach → engaged → qualifying → presenting → negotiating → closing → closed_won / closed_lost`

### Priority Order
handle_replies > send_campaigns > write_campaigns > prospect > idle (run analyst)

### Key Commands
```bash
source .venv/bin/activate    # Always activate venv first
harvey run                   # Start the heartbeat loop
harvey dashboard             # Web UI at http://localhost:5555
harvey setup                 # Re-run setup wizard
harvey train <url>           # Train on a product website
harvey status                # Pipeline summary
```

### Common Issues
- **"command not found: harvey"**: Activate venv first: `source .venv/bin/activate`
- **"externally-managed-environment"**: Use a venv, not system Python
- **SQLite errors**: The `data/` directory is created automatically on first run
- **Claude headless mode fails**: User needs `claude login` and an active Max subscription
- **Instantly API 401**: Wrong API key or needs the Growth plan for API access
- **Google search rate limiting**: Harvey automatically falls back to DuckDuckGo and Bing. For reliable search, add a Serper API key ($5/mo).
