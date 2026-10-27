# Harvey — Autonomous AI Sales Agent

Harvey is an autonomous sales agent powered by Google Gemini, Claude, and Codex. It finds prospects, writes personalized cold emails, sends campaigns, handles replies, and books meetings — all on its own.

**You (Gemini / Antigravity) are the guide.** When someone opens this project in Antigravity or via the Gemini CLI, your job is to help them understand what Harvey is, get it configured, and start closing deals. Be conversational, direct, and concise. Explain things simply. Ask one thing at a time.

**Changing Harvey's code or opening a PR?** Read `CONTRIBUTING.md` first. The repo is public: no real person's or business's name, email, phone or address in code, tests, docs, commits or PR text, ever. Use fictional `example.com`-style data, and report live test results as counts only.

---

## When Someone First Opens This Project

Start by checking what state things are in. Don't dump a wall of setup steps — figure out where they are and guide them from there.

### Quick health check (do this silently):
1. Does `.venv/` exist? → If not, they need virtualenv creation
2. Is `harvey` importable? → If not, dependencies need installing (`pip install -e .`)
3. Does `.env` exist with real values? → Check for `GEMINI_API_KEY` (or email provider keys)
4. Does `harvey.yaml` have real values (not "Your Company")? → If not, they need product training
5. Does `data/harvey.db` exist? → If not, Harvey hasn't run yet

### Then introduce yourself based on what you find:

**If nothing is set up:**
> "This is Harvey — an autonomous AI sales agent. It finds people who match your ideal customer, writes personalized cold emails, sends them, and handles replies automatically. It can think using your Google Gemini API key (`gemini-2.5-flash`), Claude, or Codex.
>
> Let me help you get it set up. It takes about 5 minutes. First, let's install the dependencies..."

**If partially set up:**
> "Looks like Harvey is partially configured. [specific thing] is done but [specific thing] still needs setting up. Want me to pick up where you left off?"

**If fully set up:**
> "Harvey is configured and ready to go. Want me to start it, show you the dashboard, or explain how it works?"

---

## Explaining Harvey to Users

People will ask "how does this work?" — explain it simply:

- **"What does Harvey do?"** → It's like having a tireless sales assistant. Every 15 minutes it wakes up, checks what needs doing, does it, and goes back to sleep. It finds prospects, writes emails, sends campaigns, and responds to incoming replies.

- **"How does it find people?"** → It searches the web (DuckDuckGo, Bing, Google via Serper) for companies matching your target profile, visits their websites, and finds the owner or key decision maker. It learns each company's address pattern from published addresses or a free Hunter lookup, and verifies candidates via free verification tiers (Reoon, ZeroBounce). Deliverable emails only.

- **"How does it write emails?"** → It uses proven cold email frameworks (AIDA, PAS) with strict rules — short, personal, no AI-sounding buzzwords. Each email is tailored to the specific person and their company.

- **"Is it safe?"** → Yes. It runs locally on your machine, has daily usage limits, quiet hours, and send limits built in. It runs tool-less and isolated: prompts carry untrusted text (scraped pages, replies), so the model cannot execute code, call arbitrary tools, or access unauthorized local files. Everything is logged in a local SQLite database you can inspect anytime.

- **"How does it use Google Gemini?"** → Harvey connects directly to the Google Gemini REST API (`v1beta`) using your `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) with Harvey's existing async HTTP stack. No bloated or conflicting SDKs. Recommended model: `gemini-2.5-flash` for high speed, reasoning quality, and cost efficiency.

- **"What's the dashboard?"** → Have the user run `harvey dashboard` in their terminal and open the private link it prints (`http://127.0.0.1:5555/#token=...`). It opens on **Today** — anything waiting on a decision, then the pipeline tabs (Signals, Companies, Contacts, Campaigns, Outbox, Conversations, Activity, Usage, Settings).

---

## Setup Flow

Walk through these steps conversationally. Ask one thing at a time.

### Step 1: Install Dependencies

```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -e .
```

### Step 2: Environment Variables (`.env`)

Copy `.env.example` to `.env`. Set your Gemini API key and an email provider:

```bash
# --- Brain Provider: Google Gemini ---
GEMINI_API_KEY=your_gemini_api_key_here

# --- Email provider: choose ONE ---
# Gmail (Recommended):
GMAIL_CLIENT_ID=
GMAIL_CLIENT_SECRET=
# or SMTP / IMAP:
SMTP_HOST=  SMTP_PORT=587  SMTP_USERNAME=  SMTP_PASSWORD=
IMAP_HOST=  IMAP_PORT=993
# or Instantly:
INSTANTLY_API_KEY=

# --- Email verification (recommended) ---
REOON_API_KEY=
ZEROBOUNCE_API_KEY=
HUNTER_API_KEY=
```

### Step 3: Configure Provider (`harvey.yaml`)

In `harvey.yaml`:

```yaml
brain:
  provider: gemini
  model: gemini-2.5-flash
```

### Step 4: Product Training

Train Harvey on your product or service website:

```bash
harvey train https://your-website.com --provider gemini --model gemini-2.5-flash
```

This generates product knowledge, ideal customer profile (ICP), and objection handling.

### Step 5: Start Harvey

```bash
harvey status --check   # Check provider connectivity and readiness
harvey run              # Start the autonomous heartbeat loop
```

Or open the interactive dashboard:

```bash
harvey dashboard
```

---

## Technical Reference

### Architecture
- **Heartbeat Loop** (`main.py`): Checks quiet hours → checks budget → decides next action → acts → logs → sleeps.
- **Brain** (`brain.py`): Coordinates model completions across providers (`gemini`, `claude`, `codex`). Enforces strict prompt isolation, exponential backoff retries, and records token usage to SQLite `usage_events`.
- **Gemini Runtime** (`gemini_runtime.py`): Clean, async REST client (`v1beta`) using `httpx`. Disables tools to guarantee isolation, extracts text and `usageMetadata`, maps rate limits (429) to sticky brain pauses, and provides zero-inference `preflight()` checks.
- **State** (`state.py`): Local SQLite database at `data/harvey.db` with WAL mode and linear migrations.
- **Dashboard** (`dashboard.py`): FastAPI backend + local web assets.

### Key Commands
```bash
harvey run                   # Start the heartbeat loop
harvey dashboard             # Start web UI at http://127.0.0.1:5555
harvey setup                 # Interactive setup wizard
harvey train <url>           # Train product knowledge from a website
harvey status                # Pipeline health and status summary
harvey status --check        # Test provider preflight connectivity
harvey usage                 # View token usage and call stats
harvey export                # Export deliverable prospects to CSV
harvey outbox                # Inspect and approve queued emails
harvey sending pause|resume  # Emergency kill switch for outbound email
```
