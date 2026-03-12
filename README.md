# Harvey

**The autonomous AI sales agent that never stops closing.**

Harvey is an open-source, fully autonomous sales agent that finds prospects, writes personalized cold email campaigns, sends them, monitors replies, handles objections, and books meetings — all without human intervention. Named after Harvey Specter from *Suits*, because the best closer in New York never sleeps.

Harvey runs on your existing Claude Max subscription (zero extra LLM cost), deploys to any VPS with Docker, and replaces expensive prospecting tools by finding leads on its own.

```
$ python -m harvey

============================================================
Harvey is online. Always Be Closing.
============================================================
Database initialized.
Thinking about what to do next...
Decision: prospect
Scout: Starting prospecting cycle...
Scout: Searching LinkedIn...
Scout: Added prospect Sarah Chen — VP Marketing at Acme SaaS
Scout: Added prospect James Rivera — Head of Growth at GrowthCo
...
```

---

## What Harvey Does

Harvey runs a continuous heartbeat loop — wake up, decide what needs doing, do it, log the results, sleep, repeat. Every 15 minutes (configurable), Harvey checks the state of your sales pipeline and takes the highest-priority action.

### The Loop

```
Wake Up → Check Budget → Decide → Act → Log → Sleep → Repeat
   ↑                                                    |
   └────────────────────────────────────────────────────┘
```

### Sub-Agents

Harvey feels like one agent, but under the hood it coordinates five specialized sub-agents:

| Agent | What It Does |
|-------|-------------|
| **Scout** | Finds prospects matching your ICP — via LinkedIn search, Google dorking, company website scraping, and email pattern discovery. No Apollo or ZoomInfo needed. |
| **Writer** | Crafts personalized 3-email sequences using proven frameworks (AIDA, PAS, BAB). Every email is tailored to the prospect's role, company, and industry. |
| **Sender** | Deploys campaigns to Instantly (or Woodpecker) via API. Adds leads, sets sequences, activates campaigns, and tracks delivery. |
| **Handler** | Monitors all replies. Classifies intent (interested, objection, not interested, OOO, wrong person) and auto-responds with context-aware messages that move toward a meeting. |
| **Analyst** | *(Coming soon)* Tracks what's working — which subject lines, frameworks, ICP segments, and send times produce the best results — and adjusts Harvey's approach automatically. |

### Skills System

Each sub-agent is loaded with foundational sales knowledge from Harvey's skills library:

| Skill | What It Teaches |
|-------|----------------|
| **Email Frameworks** | AIDA, PAS, BAB, QVC, 3Ps — when to use each, with templates and selection rules |
| **Objection Handling** | LAARC framework, responses for Budget/Authority/Need/Timing objections, advanced techniques |
| **Lead Qualification** | BANT screening, ICP scoring (1-10 scale), MEDDIC for complex deals |
| **LinkedIn Outreach** | Connection sequences, warm-up strategies, messaging templates, rate limits |
| **Prospecting Tactics** | Google dorking, company scraping, email discovery, trigger events, referral mining |
| **Sales Methodology** | Multi-channel orchestration, COSTAR tone calibration, decision priorities, ethical guidelines |

Skills are plain markdown files in the `skills/` directory. Edit them to change how Harvey sells — no code changes needed.

---

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────┐
│                HARVEY (VPS)                      │
│                                                  │
│  ┌───────────┐   Heartbeat Loop                  │
│  │ Scheduler │──► Wake → Decide → Act → Log      │
│  └───────────┘                                   │
│                                                  │
│  ┌─────────────────────────────────────────┐     │
│  │         Brain (Claude Code CLI)         │     │
│  │    claude -p (your Max subscription)    │     │
│  └─────────┬───────────────────────────────┘     │
│            │                                     │
│  ┌─────────▼───────────────────────────────┐     │
│  │            Sub-Agents                   │     │
│  │  Scout → Writer → Sender → Handler      │     │
│  └─────────────────────────────────────────┘     │
│                                                  │
│  ┌─────────────────────────────────────────┐     │
│  │          Integrations                   │     │
│  │  Instantly API │ Playwright │ SMTP      │     │
│  └─────────────────────────────────────────┘     │
│                                                  │
│  ┌─────────────────────────────────────────┐     │
│  │          State (SQLite)                 │     │
│  │  Prospects │ Campaigns │ Conversations  │     │
│  └─────────────────────────────────────────┘     │
└─────────────────────────────────────────────────┘
```

### The Brain

Harvey's brain is Claude, accessed through Claude Code's headless mode (`claude -p`). This means Harvey runs on your existing Claude Max subscription — no API keys, no per-token charges, no surprise bills. An always-running autonomous agent would cost hundreds per month on API pricing. With Max, it's included.

The brain handles all reasoning: deciding what to do next, writing emails, classifying reply intent, generating objection responses, and finding personalization angles.

### DIY Prospecting

Most sales tools charge $200+/month for lead databases. Harvey finds prospects for free:

1. **LinkedIn Search** — Playwright browser automation searches for people matching your ICP (title, industry, location). Logs into your LinkedIn account and extracts profiles with human-like behavior patterns.

2. **Google Dorking** — Targeted search queries like `site:linkedin.com/in "VP Marketing" "SaaS"` find profiles indexed by Google.

3. **Company Website Scraping** — Checks /team, /about, /people pages to find decision-makers and their roles.

4. **Email Pattern Discovery** — Given a name and company domain, Harvey tries common email patterns (first.last@, first@, flast@) and verifies them via MX record lookup and SMTP checks. No paid verification service needed.

### Usage Control

Harvey tracks its own Claude usage and lets you set a daily limit. Set `max_daily_claude_percent: 80` in your config and Harvey will stop working when it hits 80% of its daily budget — then pick back up the next morning.

Quiet hours are also configurable. Harvey won't run between 10pm and 7am (or whatever you set).

---

## Quick Start

### Prerequisites

- [Claude Code CLI](https://claude.ai/download) installed and authenticated (with a Max subscription)
- Python 3.11+
- An [Instantly](https://instantly.ai) account with API access (for cold email)
- A LinkedIn account (for prospecting — optional)

### 1. Clone and Open in Claude

```bash
git clone https://github.com/ethanplusai/harvey.git
cd harvey
claude
```

That's it. Claude reads the project, sees it's unconfigured, and walks you through everything — installs dependencies, connects your email platform, trains on your product, and gets Harvey running. Just follow along.

### 2. Run Harvey (after setup)

Once configured, start Harvey anytime:

```bash
cd harvey
source .venv/bin/activate
harvey run
```

Other commands:

```bash
harvey setup              # Re-run the setup wizard
harvey status             # Show pipeline summary
harvey train <url>        # Re-train on a new product website
harvey train <url> 500    # Crawl more pages for larger sites
```

```
╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   Harvey Setup Wizard                                    ║
║   Let's get you closing deals.                           ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝

  Harvey: Hey. I'm Harvey. Let's get me set up so I can start
  Harvey: closing deals for you. I'll walk you through everything.
  Harvey: This takes about 5 minutes.
```

The wizard walks you through 6 steps:

**Step 1 — Prerequisites Check.** Harvey verifies Claude Code CLI is installed, tests that headless mode works with your Max subscription, and checks all Python dependencies.

**Step 2 — Email Platform.** Harvey asks for your Instantly API key and tests the connection live. If it doesn't work, Harvey tells you exactly what's wrong.

**Step 3 — LinkedIn (optional).** If you want Harvey to prospect on LinkedIn, give it your credentials. If not, Harvey will use Google and company websites instead.

**Step 4 — Cloudflare Crawling (optional).** For deep website crawling with JavaScript rendering. $5/month for ~12,000 pages. Harvey works without it — just uses its built-in crawler instead.

**Step 5 — Product Training.** This is the big one. Harvey asks: *"Train from website URL or enter manually?"*

If you give Harvey your website URL, it deep-crawls the entire site and teaches itself everything:

```
  Harvey: Give me a minute — I'm going to crawl https://yourcompany.com
  Harvey: and learn everything I can about your product.

[1/6] Deep crawling with Cloudflare (up to 100 pages)...
      Crawling... 47/47 pages (34s)
      Done! Crawled 47 pages.

[2/6] Analyzing product information...
      Found: AcmeWidget

[3/6] Identifying ideal customer profile...
      Target: E-commerce, DTC Brands

[4/6] Analyzing competitive landscape...
      Identified 4 competitors.

[5/6] Generating objection handling playbook...
      Prepared 10 objection responses.

[6/6] Building configuration and product knowledge...
      Generated: skills/product_knowledge.md
      Generated: skills/competitive_intel.md
```

From your website, Harvey extracts:
- Product name, description, features, pricing, and value proposition
- Key benefits and use cases
- Ideal customer profile — industries, company sizes, decision-maker titles
- Pain points and buying triggers
- Competitor analysis with battle cards
- Brand tone and voice
- Objection handling responses specific to your product

If you don't have a website (or prefer to do it manually), Harvey asks you the questions directly — company name, product description, target audience, etc.

**Step 6 — Behavior Settings.** Harvey asks how much of your daily Claude quota it can use, how often to check for work, quiet hours, send limits, and timezone.

After setup, Harvey writes all config files for you. Just run `harvey run` to start closing.

To re-run setup anytime: `harvey setup`

### 4. Deploy (VPS)

For always-on operation, deploy with Docker:

```bash
# Make sure Claude CLI is authenticated first
claude login

# Start Harvey
docker compose up -d

# Watch the logs
docker compose logs -f harvey
```

### Re-Training on a Different Product

You can re-train Harvey anytime by pointing it at a new website:

```bash
harvey train https://newproduct.com
```

Or crawl more pages for larger sites:

```bash
harvey train https://yourcompany.com 500
```

#### Deep Crawling with Cloudflare

Harvey can use [Cloudflare's Browser Rendering /crawl API](https://developers.cloudflare.com/browser-rendering/rest-api/crawl-endpoint/) for deep website crawling:

- **JavaScript rendering** — works on SPAs, React sites, dynamic content
- **Automatic page discovery** — follows sitemaps and internal links
- **Clean markdown output** — no HTML parsing needed
- **Up to 100,000 pages** — crawl the entire site, not just key pages
- **~$5/month** for ~12,000 pages on Cloudflare's paid Workers plan

This is optional. Without Cloudflare credentials, Harvey uses its built-in recursive crawler that follows every internal link it finds. It works great for most sites — just can't render JavaScript.

#### What the Trainer Generates

| File | What's in it |
|------|-------------|
| `harvey.yaml` | Complete config — persona, product, ICP, channels, usage limits |
| `skills/product_knowledge.md` | Everything about your product — features, benefits, use cases, pricing, social proof, pain points, buying triggers, disqualifiers |
| `skills/competitive_intel.md` | Battle cards for every competitor — how you win, their weaknesses, migration angles, ready-to-use responses |

---

## Project Structure

```
harvey/
├── harvey.yaml              # Your config — persona, product, ICP, settings
├── .env                     # API keys (never committed)
├── docker-compose.yml       # One-command VPS deployment
├── Dockerfile
├── requirements.txt
│
├── harvey/
│   ├── main.py              # Heartbeat loop — the core engine
│   ├── brain.py             # Claude Code headless wrapper + skills loader
│   ├── state.py             # SQLite state manager
│   ├── config.py            # Configuration loader + validation
│   ├── setup.py             # Interactive first-run setup wizard
│   ├── trainer.py           # Auto-train Harvey from a website URL
│   │
│   ├── agents/
│   │   ├── scout.py         # DIY prospecting
│   │   ├── writer.py        # Email sequence generation
│   │   ├── sender.py        # Campaign deployment via Instantly
│   │   ├── handler.py       # Reply processing + auto-response
│   │   └── analyst.py       # (Coming soon) Learning from outcomes
│   │
│   ├── integrations/
│   │   ├── instantly.py     # Instantly API v2 client
│   │   ├── linkedin.py      # Playwright browser automation
│   │   ├── email_finder.py  # Email pattern discovery + SMTP verification
│   │   └── calendar.py      # (Coming soon) Meeting booking
│   │
│   └── models/
│       ├── prospect.py      # Prospect data model
│       ├── campaign.py      # Campaign + email sequence models
│       └── conversation.py  # Conversation thread model
│
├── prompts/                 # Prompt templates (editable)
│   ├── system.md            # Harvey's core persona
│   ├── scout.md             # Prospecting instructions
│   ├── writer.md            # Email writing guidelines
│   ├── handler.md           # Reply handling rules
│   └── qualifier.md         # Lead qualification criteria
│
├── skills/                  # Foundational sales knowledge (editable)
│   ├── email_frameworks.md
│   ├── objection_handling.md
│   ├── lead_qualification.md
│   ├── linkedin_outreach.md
│   ├── prospecting_tactics.md
│   └── sales_methodology.md
│
└── data/                    # Runtime data (auto-created)
    └── harvey.db            # SQLite database
```

---

## Customization

### Change How Harvey Writes

Edit `prompts/writer.md` to adjust email style, length, tone, or CTA approach. Edit `skills/email_frameworks.md` to change which copywriting frameworks Harvey uses.

### Change How Harvey Handles Objections

Edit `skills/objection_handling.md` to add industry-specific objection responses. Or add them directly in `harvey.yaml` under `product.objection_responses`.

### Change How Harvey Prospects

Edit `skills/prospecting_tactics.md` to adjust search strategies. Edit `skills/lead_qualification.md` to change scoring criteria.

### Add a New Channel

Create a new integration in `harvey/integrations/` and a corresponding agent in `harvey/agents/`. Wire it into the heartbeat loop in `main.py`.

---

## How Harvey Decides What To Do

Every heartbeat cycle, Harvey checks the pipeline state and picks the highest-priority action:

| Priority | Action | Trigger |
|----------|--------|---------|
| 1 | **Handle replies** | Open conversations with new messages |
| 2 | **Send campaigns** | Draft campaigns ready to deploy |
| 3 | **Write campaigns** | New prospects without campaigns |
| 4 | **Prospect** | Fewer than 20 prospects with status "new" |
| 5 | **Idle** | Everything is running, nothing needs attention |

Hot leads cool fast — that's why reply handling is always #1.

---

## Database

Harvey stores everything in SQLite (`data/harvey.db`):

| Table | What It Stores |
|-------|---------------|
| `prospects` | Everyone Harvey has found — name, email, company, title, status, score, personalization notes |
| `campaigns` | Email sequences — the emails, which prospects are in each campaign, deployment status |
| `conversations` | Full conversation threads — every message sent and received, classified intent |
| `actions` | Audit log — everything Harvey has done, when, and the result |
| `usage_log` | Daily Claude usage tracking for budget control |

---

## Roadmap

- [x] Core heartbeat loop
- [x] DIY prospecting (LinkedIn + Google + email discovery)
- [x] Email sequence generation with copywriting frameworks
- [x] Instantly integration for campaign deployment
- [x] Reply handling with intent classification
- [x] Skills system for foundational sales knowledge
- [x] Usage tracking and daily limits
- [x] Docker deployment
- [x] Website trainer — point Harvey at a URL and it learns the product automatically
- [x] Deep crawling via Cloudflare Browser Rendering API (with built-in fallback)
- [x] Competitive intelligence — auto-generated battle cards per competitor
- [x] Interactive setup wizard — Harvey walks you through everything on first run
- [ ] LinkedIn DM outreach
- [ ] Calendar integration (Cal.com / Calendly) for auto-booking
- [ ] AI voice cold calling (Bland.ai / Vapi)
- [ ] SMS/text outreach
- [ ] Analyst agent — learn from outcomes, A/B test messaging
- [ ] Web dashboard — see pipeline, review Harvey's work, override decisions
- [ ] Multi-product support — run Harvey for multiple products simultaneously
- [ ] Team mode — multiple Harveys coordinating across territories
- [ ] Webhook receiver for real-time reply processing

---

## Philosophy

Harvey is built on a few core beliefs:

**1. Autonomous doesn't mean reckless.** Harvey has quiet hours, usage limits, rate limiting, and ethical guidelines baked in. It respects opt-outs immediately, never spams, and stops when told to.

**2. Expensive tools are optional.** Most sales teams pay $500+/month for prospecting tools, email platforms, and enrichment services. Harvey does prospecting for free, uses your existing email tool, and runs on your existing Claude subscription.

**3. Quality beats quantity.** Harvey doesn't blast 10,000 emails. It finds the right people, writes genuinely personalized outreach, and focuses on starting real conversations.

**4. The best sales agent sounds like a person, not a bot.** Harvey's writing is consultative, direct, and human. No jargon, no "I hope this finds you well," no walls of text.

**5. Everything is editable.** Prompts, skills, config — it's all plain text files. You don't need to be a developer to change how Harvey sells.

---

## License

MIT

---

*"Put that coffee down. Coffee is for closers."* — Blake, Glengarry Glen Ross
