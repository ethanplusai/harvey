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

- Python 3.11+
- [Claude Code CLI](https://claude.ai/download) installed and authenticated (with a Max subscription)
- An [Instantly](https://instantly.ai) account with API access (for cold email)
- A LinkedIn account (for prospecting)

### 1. Clone and Install

```bash
git clone https://github.com/ethanplusai/harvey.git
cd harvey
pip install -r requirements.txt
playwright install chromium
```

### 2. Train Harvey on Your Product

Point Harvey at your website and it learns everything automatically:

```bash
python -m harvey.trainer https://yourcompany.com
```

Harvey will:
1. Crawl up to 15 key pages (homepage, pricing, features, about, etc.)
2. Extract your product info, benefits, pricing, and value proposition
3. Identify your ideal customer profile (industries, titles, company size)
4. Generate objection handling responses specific to your product
5. Create a `product_knowledge.md` skill file with everything it learned
6. Write a complete `harvey.yaml` config ready to use

```
============================================================
  Harvey Training Mode
  Learning from: https://yourcompany.com
============================================================

[1/5] Crawling website...
      Scraped 12 pages.

[2/5] Analyzing product information...
      Found: AcmeWidget

[3/5] Identifying ideal customer profile...
      Target: E-commerce, DTC Brands

[4/5] Generating objection handling playbook...
      Prepared 7 objection responses.

[5/5] Building configuration...

============================================================
  Training complete!
  Config written to: harvey.yaml
============================================================
```

Review the generated `harvey.yaml` and tweak anything that needs adjusting. The trainer gets you 90% of the way there.

### 3. Add Credentials

Copy the example environment file and add your credentials:

```bash
cp .env.example .env
```

Edit `.env`:
```
INSTANTLY_API_KEY=your_key_here
LINKEDIN_EMAIL=your_linkedin_email
LINKEDIN_PASSWORD=your_linkedin_password
```

Edit `harvey.yaml` with your product, persona, and ICP:

```yaml
persona:
  name: "Harvey"
  company: "Acme Corp"
  role: "Business Development"
  email: "harvey@acmecorp.com"
  linkedin: "linkedin.com/in/harvey-acme"
  tone: "professional, consultative, confident"

product:
  name: "AcmeWidget"
  description: "AI-powered widget optimization for e-commerce teams"
  pricing: "Starting at $99/mo"
  key_benefits:
    - "Increases conversion rates by 30%"
    - "Reduces manual work by 10 hours/week"
    - "Integrates with Shopify, WooCommerce, BigCommerce"
  objection_responses:
    "too expensive": "Most clients see ROI within 30 days..."
    "already have a solution": "What's the one thing you wish it did better?"

icp:
  industries: ["E-commerce", "DTC Brands"]
  company_size: "10-200 employees"
  titles: ["VP Marketing", "Head of Growth", "E-commerce Director"]
  geography: ["United States"]

channels:
  email:
    enabled: true
    provider: "instantly"
    max_daily_sends: 50
  linkedin:
    enabled: true
    max_daily_connections: 20
    max_daily_messages: 10

usage:
  max_daily_claude_percent: 80
  heartbeat_interval_minutes: 15
  quiet_hours:
    start: "22:00"
    end: "07:00"
    timezone: "America/New_York"
```

### 4. Run

```bash
python -m harvey
```

Harvey will:
1. Initialize the database
2. Start the heartbeat loop
3. Begin prospecting, writing campaigns, and handling outreach automatically

### 5. Deploy (VPS)

For always-on operation, deploy with Docker:

```bash
# Make sure Claude CLI is authenticated first
claude login

# Start Harvey
docker compose up -d

# Watch the logs
docker compose logs -f harvey
```

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
