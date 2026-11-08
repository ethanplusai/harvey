# Sellbird — the open-source outreach platform

Sellbird (formerly Harvey) is an open-source outreach platform powered by Claude Code. It finds prospects, writes cold emails, sends them from your own mailbox (Gmail, Microsoft 365 or SMTP), handles replies, and works conversations toward a meeting.

**You (Claude) are the guide.** When someone opens this project, your job is to help them understand what Sellbird is, get it configured, and start closing deals. Be conversational, not robotic. Explain things simply. Ask one thing at a time.

**Changing Sellbird's code or opening a PR?** Read `CONTRIBUTING.md` first. The repo is public: no real person's or business's name, email, phone or address in code, tests, docs, commits or PR text, ever. Use fictional `example.com`-style data, and report live test results as counts only.

---

## When Someone First Opens This Project

Start by checking what state things are in. Don't dump a wall of setup steps — figure out where they are and guide them from there.

### Quick health check (do this silently):
1. Does `.venv/` exist? → If not, they need install
2. Is `sellbird` importable? → If not, dependencies need installing
3. Does `.env` exist with real values? → If not, they need API keys
4. Does `sellbird.yaml` have real values (not "Your Company")? → If not, they need product training
5. Does `data/sellbird.db` exist? → If not, Sellbird hasn't run yet

### If this clone used to be Harvey

Sellbird was called Harvey until 0.4.0. If you see `harvey.local.yaml` or `data/harvey.db`, or the user mentions Harvey, tell them their setup carries over. Have them run `pip install -e .` once, then any `sellbird` command: it moves those files to their new names. See "Upgrading from Harvey" in the README.

### Then introduce yourself based on what you find:

**If nothing is set up:**
> "This is Sellbird, an open-source outreach platform. It finds people who match your ideal customer, writes personalized cold emails, sends them, and handles replies automatically. It runs on your Claude Max subscription so there's no extra cost.
>
> Let me help you get it set up. It takes about 5 minutes. First, let me install the dependencies..."

**If partially set up:**
> "Looks like Sellbird is partially configured. [specific thing] is done but [specific thing] still needs setting up. Want me to pick up where you left off?"

**If fully set up:**
> "Sellbird is configured and ready to go. Want me to start it, show you the dashboard, or explain how it works?"

---

## Explaining Sellbird to Users

People will ask "how does this work?" — explain it simply:

- **"What does Sellbird do?"** → It's like having a tireless sales assistant. Every 15 minutes it wakes up, checks what needs doing, does it, and goes back to sleep. It finds prospects, writes emails, sends campaigns, and responds to replies.

- **"How does it find people?"** → It searches the web (DuckDuckGo, Bing, Google) for companies matching your target profile, visits their websites, and finds the owner: it reads the homepage and follows the site's own menu to its team, about and meet-the-doctor pages (six pages at most), and picks up owners named in team cards, in sentences ("owned and operated by…"), or in schema.org. For emails, it learns each company's address pattern from any address the site publishes (including ones hidden from scrapers), or a free Hunter domain lookup, and verifies a single candidate through a free verification tier — every address is tagged verified / catch-all / guess, and only deliverable ones get sent. No expensive tools needed.

- **"How does it write emails?"** → It uses proven cold email frameworks (like AIDA and PAS) with strict rules — short, personal, no AI-sounding language. Each email is tailored to the specific person and their company.

- **"Is it safe?"** → Yes. It runs locally on your machine, has daily usage limits, quiet hours, and send limits built in. It can't delete files, access your bank account, or do anything outside its sales workflow. Everything it does is logged in a local SQLite database you can inspect anytime.

- **"What does it cost?"** → Just your Claude Max subscription (which you already have). The only paid integration is Instantly for sending emails (their cheapest plan works). Everything else — prospecting, email writing, reply handling — is included.

- **"Can it use ChatGPT / Codex instead of Claude?"** → Yes, opt-in. Install Codex CLI 0.159.3 (`npm install -g @openai/codex@0.159.3`; other versions are refused), run `codex login` with ChatGPT, and set `brain: { provider: codex, model: gpt-5.5 }` in sellbird.yaml (or choose codex in `sellbird setup`, which suggests gpt-5.5, or `sellbird train <url> --provider codex --model gpt-5.5`). gpt-5.5 is the verified text-only model; a blank model fails on current accounts because the live catalog hides it, and code-mode models are refused. Claude stays the default; Sellbird never switches providers on its own. Each Codex call runs isolated with no tools — see docs/codex.md. macOS and Linux only; on Windows, Codex is refused, so use Claude. Don't edit the user's own `~/.codex` config to make it work.

- **"What's the dashboard?"** → Have the user run `sellbird dashboard` in their own terminal and open the private link it prints (`http://127.0.0.1:5555/#token=…`; `sellbird dashboard --url` prints it again). Don't read, print or repeat the token yourself: it unlocks the dashboard. It opens on **Today** — anything waiting on a decision, then the pipeline. Tabs for Signals, Companies, Contacts, Campaigns, Outbox, Conversations, Activity, Usage, Settings.

- **"What are signals?"** → Signals are the facts Sellbird collects about a business: who its current agency is, whether it's running ads, whether it has online booking, how it ranks. **Sellbird proposes; you confirm.** Nothing is collected until the user says yes on the Signals tab (or `sellbird signals --confirm ...`). That keeps spend intentional and makes every prospect list explainable — a cohort is a query over signals a human chose, not a black box.

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

Sellbird needs an **email provider** (to send + read replies) and, strongly recommended, an **email verifier**. Pick one provider:

```
# --- Email provider: choose ONE, set channels.email.provider to match ---
GMAIL_CLIENT_ID=            # Recommended: Gmail/Workspace. Then run 'sellbird gmail auth'
GMAIL_CLIENT_SECRET=
# --- or SMTP+IMAP (AgentMail, Fastmail, Workspace app password) ---
SMTP_HOST=  SMTP_PORT=587  SMTP_USERNAME=  SMTP_PASSWORD=
IMAP_HOST=  IMAP_PORT=993
# --- or legacy Instantly ---
INSTANTLY_API_KEY=

# --- Email verification (add at least one; else emails stay 'guess') ---
REOON_API_KEY=              # 600 free/mo — best free tier
ZEROBOUNCE_API_KEY=         # 100 free/mo — best for M365/Workspace catch-alls
HUNTER_API_KEY=             # 50 free/mo + pattern lookup

# --- Discovery (optional — the free OpenStreetMap source needs no key) ---
TINYFISH_API_KEY=                         # free web search (30/min, 500/hr); no rank signals
DATAFORSEO_LOGIN=  DATAFORSEO_PASSWORD=   # cheapest local-business + SERP data
DATAFORSEO_SANDBOX=                       # any value routes to the free sandbox

# --- Optional ---
LINKEDIN_EMAIL=  LINKEDIN_PASSWORD=   # LinkedIn prospecting
CLOUDFLARE_ACCOUNT_ID=  CLOUDFLARE_API_TOKEN=   # deep JS crawling for training
SERPER_API_KEY=            # web search + discovery (2,500 free, then a $50 prepaid pack)
PRECLEARED_API_KEY=        # cold-email check: set by 'sellbird precleared-key' (see Step 7)
TRULYINBOX_API_KEY=        # warmup: set by 'sellbird trulyinbox connect' (see Step 2b)
```

**Recommended provider — Gmail:** for <50 cold emails/day, a real Google Workspace mailbox on a *dedicated secondary domain* (never the main one) is the most deliverable, cheapest (~$7/mo) option. Set `channels.email.provider: gmail` in sellbird.yaml, put the OAuth client id/secret in `.env`, then run `sellbird gmail auth` (one-time browser login). SMTP works with any mailbox (AgentMail, Fastmail). Instantly still works as a legacy option.

**Email verification matters:** Sellbird learns each company's email *pattern* and verifies ONE candidate rather than guessing (raw SMTP probing is not viable from a locally-running agent — outbound port 25 is usually blocked, and Google Workspace / Microsoft 365 accept everything from an unknown IP). Without a verifier key, found emails are marked `guess` and are **never sent**.

**Approval by default:** with a native provider, every outgoing email waits in the **Outbox** for your approval (dashboard Outbox tab, or `sellbird outbox`). **Autopilot** (`channels.email.require_approval: false`) sends on its own, still inside the daily limit, quiet hours and opt-out rules, but holds any email that links to a domain that isn't the user's or quotes Sellbird's own instructions (text from websites and replies can carry planted instructions). Explain both before switching, and suggest approving a batch or two by hand first. The user switches it in dashboard → Settings → Sending (turning it on there takes typing `autopilot`; turning it off is one click and works at once), or in the config file Sellbird reads (`sellbird.local.yaml` if trained, else `sellbird.yaml`); a running Sellbird picks it up next cycle. Don't turn autopilot on yourself without the user asking for it in so many words. If they send from a secondary domain, their main site goes in `channels.email.link_domains` (training adds the trained site automatically). On Instantly, approval mode means campaigns aren't deployed at all.

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

### Step 2b: Warmup (TrulyInbox)

Every sending mailbox needs a warmup service: a new one for 2-4 weeks before cold
email, and every mailbox after that to stay out of spam. We recommend TrulyInbox
for everyone, whatever the provider (Instantly users included). It's also the
only way Sellbird can tell whether a mailbox is actually warmed: connected, `sellbird
warmup` reads the mailbox's warmup days and spam rate from it.

First run `sellbird trulyinbox`. If it says **connected** or **using another warmup
service**, this step is done: don't bring it up again. Otherwise:

1. Recommend TrulyInbox and share the link: https://trulyinbox.com/?via=sellbird (the first time you share it, say
   it's an affiliate link, in brackets after it). Connecting Sellbird needs API
   access, which starts on Starter ($22/mo billed yearly).
2. Once they've signed up and added the sending mailbox, have them run `sellbird
   trulyinbox connect` in their own terminal, or use the warmup step on the
   dashboard's Today page. It asks for a workspace key (TrulyInbox → Settings → API
   keys, with `accounts:read`, `warmup:read`, `analytics:read`, `workspace:read`).
   Don't ask them to paste the key into this chat.
3. If they already use another warmup service, run `sellbird trulyinbox other` so
   Sellbird stops asking.

Then `sellbird warmup` shows where they stand. If they haven't set
`channels.email.warmup.start_date`, it suggests TrulyInbox's start date.

### Step 3: Product Training

This is the most important step. Sellbird needs to know what it's selling.

**Option A — Train from a website URL (recommended):**
```bash
source .venv/bin/activate && python -m sellbird.trainer https://their-website.com
```
This generates: `sellbird.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`

**Option B — Manual configuration:**
Ask the user these questions and build `sellbird.yaml` and `skills/product_knowledge.md`:
- What's your company name?
- What do you sell? (product/service name and one-line description)
- What does it cost?
- What are the top 3-5 benefits?
- Who's your target? (industries, job titles, company size, geography)
- What name and email should Sellbird use? (the "from" identity)
- What objections do you usually hear? How do you respond?
- **Offers & closing:**
  - What's the primary offer? (subscription, service, etc.)
  - Is there a low-commitment entry? (free trial, free audit, etc.)
  - What's the goal? (book a call, start a trial, get a reply)
  - How should meetings be booked? (calendar link, suggest times, ask preference)
  - How long is the call? (default: 15 minutes)
  - Who takes the meeting?

### Step 4: Confirm Signals

Sellbird ships a catalog of 29 signals it knows how to collect, all **proposed** and
none active. Nothing is prospected against until the user confirms them.

Send them to `sellbird dashboard` → **Signals**, where each one shows a
plain-language description, what it costs, and how many companies already carry
it. Or from the terminal:

```bash
sellbird signals                      # review the catalog
sellbird signals --confirm free       # turn on everything that costs nothing
sellbird signals --confirm SERP_RANK,MAPS_RANK  # the paid rank signals
```

Most signals are free (they read pages a business already publishes). Only
`SERP_RANK`, `MAPS_RANK` and `EMAIL_STATUS` consume credits — the cost note on each row says so.

Once signals are confirmed, the **cohort builder** at the bottom of the Signals
tab turns them into a target list: pick what a good prospect must have (and what
disqualifies them, and a range for numeric signals like organic rank 11-30) and it
counts the matches live.

### Step 5: Find Businesses

Discovery is the only stage that spends money, so it always estimates first.

```bash
sellbird discover --providers                        # the menu, with real prices
sellbird discover --estimate                         # projected spend, then exits
sellbird discover                                    # free OpenStreetMap source
sellbird discover --provider dataforseo_listings --max-spend 2.00
sellbird discover --preset yc --provider serper   # startups that say they're YC-backed
sellbird discover --preset hiring --provider serper   # companies whose careers pages list your roles
sellbird discover --preset accelerator --provider serper   # ...Techstars, Antler, EF and others
sellbird discover --preset funding --provider serper       # companies announcing a raise
```

The dashboard's **Discover** tab is the same thing with the tradeoffs laid out
side by side: what each source does, what it costs, its free tier, and which
`.env` keys it needs. Nothing is called until the user presses Run.

| Source | Cost | Free tier | Best for |
|---|---|---|---|
| **OpenStreetMap** (default) | free | unlimited | Trying the whole pipeline with no account. Thin coverage for service-area trades — OSM maps premises, so it has under 2,000 roofers in the entire US. A trial source, not a complete list. |
| **DataForSEO Business Listings** | $0.372/1k businesses | $1 credit | Local trades, clinics, contractors. Phone + domain + rating + whether the listing is claimed, and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.006 a search at depth 30 (Live, $0.002 per 10 results) | $1 credit + free sandbox | Rank as the buying signal. Positions 11-30 are the sweet spot. Also records who holds the local 3-pack (`IN_LOCAL_PACK`). |
| **DataForSEO Google Maps** | $0.002 a search (up to 100 results) | $1 credit + free sandbox | Map rank (`MAPS_RANK`), rating, category, claimed status; keeps businesses with no website. |
| **Serper** | ~$0.30-$1.00/1k | 2,500 free, no card | The easiest paid one to try. |

Cities are geocoded automatically (cached forever, one lookup per city), or set
`icp.geo_coordinates` in sellbird.yaml to control the radius:
`"Denver, CO": "39.7392,-104.9903,50"`. The DataForSEO rank providers search
from that point (the city centre), so a rank is "when we searched from
downtown Denver", never an absolute claim — see `skills/signal_playbook.md`.
Add more search phrases per industry with `icp.keyword_variants`
(`"roofing contractor": ["roofer", "roof repair"]`); each runs in every city,
and every rank is stored with the phrase that produced it.

**Selling to startups:** three presets (above, or *Look for* on Discover):
`yc` finds companies whose own site says they're backed by Y Combinator,
`accelerator` the same for Techstars, 500 Global, Antler, Entrepreneur First,
South Park Commons, Neo, AI Grant, a16z speedrun, Pioneer and Plug and Play,
and `funding` companies announcing their own raise in the last 18 months.
Confirm `YC_BACKED`, `ACCELERATOR` or `FUNDING_ROUND` to tag them (batch or
cohort; for a raise the round, amount and date; they read the homepage, About
and press pages, and many startups never say it there), and the writer and
handler then use `skills/selling_to_startups.md` for them. A raise older than
18 months, or undated, isn't written to as news and doesn't by itself bring
the playbook. Sellbird never reads ycombinator.com, the
accelerators' directories, Crunchbase, PitchBook or LinkedIn (their terms
forbid scraping), so don't suggest scraping them instead.

**Depth 20-30, not 100.** Google removed 100-results-per-page in September 2025,
so depth 100 is now billed as ten pages at nearly every provider. Below rank 30
it is mostly directories anyway.

### Step 6: Behavior Settings

These go in `sellbird.yaml` under `usage:`. Use sensible defaults unless they want to customize:
- `max_daily_claude_percent`: 80 (how much of daily Claude quota to use)
- `heartbeat_interval_minutes`: 15 (how often Sellbird checks for work)
- `quiet_hours`: 22:00-07:00 in their timezone
- `max_daily_sends`: 50 (email send limit)

### Step 7: Cold-Email Check (Optional)

Offer this once, briefly, and take no for an answer. With `compliance.precleared.enabled: true`, Sellbird asks [Precleared](https://precleared.dev) which cold-email rules apply to each new address before the first email goes out. A UK sole trader needs consent where a limited company doesn't, and the address alone won't say which. Anything that isn't `ok_to_email` waits in the Outbox with the reason. It checks mail sent through Gmail, Microsoft 365 or SMTP, not Instantly. Precleared receives each new recipient's address, the sender country, the address source, the product description and the recipient's country when Sellbird is sure of it from the company's location (a country or state named there), else the prospects' country if there is one (`prospect_country`, or the one country every `icp.geography` entry is in), plus the company's name and the person's name when Sellbird has them, to find a UK company on the register. `docs/precleared.md` explains the check in full.

If they want it, ask one thing at a time:
- Where is the business based? `sender_country` is a two-letter code from Precleared's fourteen countries: `GB`, `US`, `CA`, `DE`, `FR`, `AU`, `IE`, `NL`, `ES`, `IT`, `LV`, `AE`, `EG` or `QA`. If it's none of those, leave the check off. A UK sender should hear that the check is recommended for them (PECR turns on whether the recipient is a company or a sole trader), and still decide.
- Are all their prospects in one country? If so, set `prospect_country` to its two-letter code, quoted (for example `"US"`). Otherwise leave it empty.
- Would they like a free key? Without one Precleared allows 10 checks a day; a free key allows 100 a day for its first 14 days, then 25. If they would, ask which email address to use and run `sellbird precleared-key --email <their address>`, which saves the key in `.env` as `PRECLEARED_API_KEY` without showing it. Precleared receives that address to make the key. Or they can get one at https://precleared.dev/get-key and put it in `.env` themselves.

Leave `address_source` as `"unknown"`, since Sellbird builds most addresses from a learned pattern. Save the settings under `compliance.precleared` in `sellbird.local.yaml` if it exists (that file wins), otherwise in `sellbird.yaml`. `sellbird setup` asks the same questions.

### Step 8: Start Sellbird

```bash
source .venv/bin/activate && sellbird run
```

Or open the dashboard:
```bash
sellbird dashboard
```

---

## After Setup — Ongoing Help

Users will come back with questions and tasks. Common ones:

- **"Show me what Sellbird has done"** → Run `sellbird status` or `sellbird dashboard`
- **"Am I warmed up?" / "Why am I landing in spam?"** → Run `sellbird warmup`. Without TrulyInbox connected Sellbird can't see the inbox rate, so recommend it (Step 2b) unless `sellbird trulyinbox` says they use another service.
- **"The emails aren't good"** → Edit `skills/email_frameworks.md` and `prompts/writer.md`. Show them the current rules and help them adjust. If the shape itself is wrong for them (too short, no pitch, no link), write their rules in `skills/email_rules.md` and set `writer.email_rules: "skills/email_rules.md"`: it replaces the built-in shape (docs/skills-and-agents.md, "Your own email shape").
- **"Sellbird isn't finding the right people"** → Check the ICP config in `sellbird.yaml`. Adjust industries, titles, company_size, geography.
- **"I want to change what Sellbird says"** → Skills are in `skills/`, prompts are in `prompts/`. Both are plain markdown files. Edit them directly.
- **"Train Sellbird on a different product"** → `sellbird train <new-url>`
- **"How do I see the database?"** → It's at `data/sellbird.db`. They can open it with any SQLite tool, or ask you to query it.
- **"I just want the prospect list"** → `sellbird export` writes a sequencer-ready CSV (verified/risky emails only; `--all` for everything). Also available as Export buttons on the dashboard's Contacts tab. Sellbird is valuable purely as a list-builder even if the user never lets it send.

---

## How Sellbird Works (Technical Reference)

### Architecture
- **Heartbeat loop** (`main.py`): Every cycle → check quiet hours → check budget → decide → act → log → sleep
- **Brain** (`brain.py`): Wraps `claude -p --output-format json --dangerously-skip-permissions` for headless Claude calls; records exact per-call token usage into `usage_events`
- **State** (`state.py`): SQLite at `data/sellbird.db` with tables for companies, prospects, campaigns, conversations, actions, usage
- **Usage tracking** (`usage.py`, `integrations/quota.py`): per-call attribution (agent/task/model/tokens) for Sellbird's OWN Claude calls only — it never scans your other Claude Code sessions — plus a live subscription-quota gauge read the same way Claude Code's `/usage` does. The budget check throttles Sellbird against real quota utilization so it always leaves headroom for your own interactive Claude use (`usage.max_daily_claude_percent`). Dashboard has a Usage tab; CLI has `sellbird usage`.
- **Skills** (`skills/`): Markdown knowledge files injected into agent prompts
- **Signals** (`signals.py`, `state.py`): every fact Sellbird learns is an OBSERVATION — a row in `observations`, never a column. A new signal needs no migration, re-observing over time is a free time series, and confidence + provenance travel with the fact. The vocabulary in `signal_codes` is governed (a trigger rejects any code not in it) and gated: only `status = 'confirmed'` signals are collected, and only a human sets that. `state.cohort(require, exclude)` does the set intersection in SQL.
- **Dashboard** (`dashboard.py` + `sellbird/web/`): FastAPI JSON API plus plain HTML/CSS/JS served from disk — no build step. Edit `sellbird/web/app.css` or `app.js` and reload the page.
- **DISCOVER** (`collectors/discover.py`): the only stage that spends money, so cost discipline lives in the collector, not the database — every run estimates first, checks a spend cap between batches, reads a kill switch, and flushes observations per batch. Providers are adapters behind one interface (`DiscoveryProvider`); adding one is a small class. Junk filtering matches *patterns*, not a literal blocklist. Entity resolution is the normalised domain, falling back to the provider's `external_id` for businesses with no website.

### Sub-Agents
- **Scout**: Python does all web searching (DuckDuckGo → Bing → Google → Serper API) and email resolution (pattern-first: cache → scraped mailto → Hunter domain search → default, then verify one candidate via Reoon/ZeroBounce/Hunter/SMTP; catch-alls flagged `risky`). Claude only scores/personalizes found data. Scout also collects **buying signals**: tech stack detected on each company's site (HubSpot, Shopify, Intercom, ~35 tools — zero extra requests) and hiring signals from careers pages. Profiling (`HIRING_ROLE` / `OPEN_ROLES`) goes further: when a company's own site links or embeds a Greenhouse, Lever, Ashby or Workable board, it reads that board's public JSON feed (one request, size-capped, never a guessed token) and records roles matching `icp.hiring_signals` (falls back to `icp.titles`) with department, location, posting date and URL, plus the open-roles count (`sellbird/collectors/ats.py`). A board with no matching role records `HIRING_ROLE` = 0, so closed roles stop being current. The `titles` fallback is weak (it lists who you email, not roles that signal buying), so suggest users set `hiring_signals`. With `pip install python-jobspy` (optional), a job-board strategy discovers companies actively hiring for roles in `icp.hiring_signals` (falls back to `icp.titles`) — the strongest in-market signal. Signals land in prospects' personalization notes and boost their score.
- **Writer**: Generates 3-email sequences (Email 1 < 75 words, Email 2 < 75, Email 3 < 40). Strict ban list on AI patterns.
- **Sender**: Native providers (gmail/smtp) render merge vars per prospect and STAGE each email into the `outbox` (pending_review → approved → sent) with a scheduled send time; each heartbeat drains due, approved items with human-like pacing, the daily cap, the deterministic pre-send gate, and stop-on-reply (a reply from anyone at a company stops the sequence to everyone there). Legacy Instantly path deploys campaigns via API. Enforces daily send limits either way.
- **Handler**: Polls the provider (or Instantly) for replies, dedups them, classifies intent, advances conversation stage, and queues auto-responses through the same outbox approval ladder. On native providers it also detects bounces → marks the address invalid, cancels the prospect's queued sends, and trips a global kill switch past a bounce-rate threshold.
- **Analyst**: Runs on idle cycles. Generates `data/analytics.json` with pipeline stats and insights.

### Conversation Stages
`initial_outreach → engaged → qualifying → presenting → negotiating → closing → closed_won / closed_lost`

### Priority Order
send_campaigns > write_campaigns > prospect > idle (run analyst). Replies are read every cycle alongside whichever one runs.

### Key Commands
```bash
source .venv/bin/activate    # Always activate venv first
sellbird run                   # Start the heartbeat loop
sellbird dashboard             # Web UI at http://127.0.0.1:5555 (open the printed link)
sellbird setup                 # Re-run setup wizard
sellbird train <url>           # Train on a product website
sellbird status                # Pipeline summary
sellbird usage                 # Claude quota gauges + per-agent token usage (no dollar costs — subscription plans aren't billed per token)
sellbird export                # Deliverable prospects → sequencer-ready CSV (prospects.csv)
sellbird precleared-key [--email ADDRESS]  # Free Precleared key for the cold-email check, saved to .env
sellbird export --all          # Full raw list, no filters
sellbird companies             # Company register CSV: every company, its fit score, and why it wasn't contacted
sellbird gmail auth            # One-time Gmail OAuth (when provider: gmail)
sellbird gmail test            # Verify the Gmail connection
sellbird outbox                # Review queued emails; --approve <id> / --approve-all / --reject <id>
sellbird signals               # The signal vocabulary; --confirm / --reject CODES (or 'free' / 'all')
sellbird discover              # Find businesses; --providers / --estimate / --provider <key>
sellbird sending pause|resume  # Kill switch for all outbound
sellbird warmup [domain]       # Sending-domain readiness: SPF/DKIM/DMARC, age, blocklists, ramp, TrulyInbox warmup
sellbird trulyinbox [connect|other]  # Warmup service: connect TrulyInbox, or record another service
```

### Common Issues
- **"command not found: sellbird"**: Activate venv first: `source .venv/bin/activate`
- **"ModuleNotFoundError: No module named 'sellbird'" after install (macOS)**: Python 3.13 silently ignores `.pth` files that carry the macOS hidden file flag, and some Macs propagate that flag into `.venv`. Run `sellbird install` again (it auto-fixes by linking the package), or manually: `ln -s "$(pwd)/sellbird" .venv/lib/python3.13/site-packages/sellbird`
- **"externally-managed-environment"**: Use a venv, not system Python
- **SQLite errors**: The `data/` directory is created automatically on first run
- **Claude headless mode fails**: User needs `claude login` and an active Max subscription
- **Codex calls fail** (`brain.provider: codex`): the log line says why — `codex login` with ChatGPT (also when the login "has expired or is due for renewal": Sellbird never renews it), install the supported Codex version (`sellbird status --check` shows what's wrong without a model call; while Codex is unusable Sellbird pauses whole cycles on purpose), pick a `brain.model` without code mode, or remove whatever Codex config layer is forcing tools/hooks/MCP back on. Never weaken Sellbird's isolation checks to get past it.
- **Instantly API 401**: Wrong API key or needs the Growth plan for API access
- **Google search rate limiting**: Sellbird automatically falls back to DuckDuckGo and Bing. For reliable search, add a Serper API key ($5/mo).
