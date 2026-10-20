# Harvey

**An autonomous sales agent that runs on your Claude Code, Codex, or Antigravity subscription.**

Harvey finds businesses worth selling to, learns something specific about each one, writes cold email that references it, sends it, reads the replies, and works the conversation toward a meeting. It runs on your machine, in a loop, on its own.

The unusual part: **there is no model API bill.** Harvey uses your Claude Pro or Max subscription through Claude Code, your ChatGPT subscription through Codex, or your Antigravity subscription through the Antigravity CLI (`agy`). Model calls use the plan you already pay for, without an API key or per-token API billing. Claude is the default; [Codex](#using-a-chatgpt-plan-instead-codex) and [Antigravity](#using-an-antigravity-subscription-agy) are opt-in providers.

```
$ harvey run

============================================================
Harvey is online. Always Be Closing.
============================================================
Checking pipeline state...
Decision: prospect (only 3 new prospects; pipeline needs leads)
  discover  → 41 businesses in Portland, OR       ($0.00, OpenStreetMap)
  profile   → 41 sites read, 288 observations     (free, no model calls)
  scout     → 12 scored against your ICP
Decision: write_campaign (12 new prospects with no drafts)
  writer    → 3-email sequence for Cascade Landscaping
  outbox    → 12 emails waiting for your approval
Cycle complete. Sleeping for 15 minutes.
```

And the dashboard it keeps, at `localhost:5555` — confirming a signal, building a
cohort out of the confirmed ones, pricing a discovery run before it spends
anything, and approving mail one message at a time:

![Harvey's dashboard: the Today queue, confirming a signal, the cohort builder counting matches live, a costed discovery estimate, and the outbox decisions desk — in light and dark](docs/dashboard.gif)

---

## Table of contents

- [Why the subscription thing matters](#why-the-subscription-thing-matters)
- [What Harvey actually does](#what-harvey-actually-does)
- [Quick start](#quick-start)
- [You confirm what it looks for](#you-confirm-what-it-looks-for)
- [The prospecting pipeline](#the-prospecting-pipeline)
- [The skills library](#the-skills-library)
- [The sub-agents](#the-sub-agents)
- [Nothing sends without you](#nothing-sends-without-you)
- [The dashboard](#the-dashboard)
- [How the data is stored](#how-the-data-is-stored)
- [Configuration](#configuration)
- [Using a ChatGPT plan instead (Codex)](#using-a-chatgpt-plan-instead-codex)
- [Using an Antigravity subscription (agy)](#using-an-antigravity-subscription-agy)
- [Legal and deliverability](#legal-and-deliverability)
- [Troubleshooting](#troubleshooting)
- [Project structure](#project-structure)
- [Philosophy](#philosophy)
- [Roadmap](#roadmap)

---

## Why the subscription thing matters

Every other autonomous sales agent bills you per token. That is the whole reason they cost what they cost: an agent that thinks in a loop, all day, is an agent that burns API credits in a loop, all day. Vendors solve this by thinking less — shorter prompts, cheaper models, fewer passes.

Harvey uses the subscription-backed CLI for the provider you choose: `claude` for Claude Code, or an isolated `codex app-server` for Codex. Both return text with model tools disabled. Harvey handles the prospecting and mail operations itself.

Claude calls run with none of your own Claude Code setup: no `CLAUDE.md` (yours, the project's or any parent folder's), no hooks, skills, plugins, MCP servers or settings files, no tools, in an empty temporary folder, and nothing saved to your Claude transcripts. Your login (subscription or `claude setup-token`) and any admin-managed policy still apply. This needs a Claude CLI with `--safe-mode`; with an older one Harvey pauses instead of sending prompts unisolated and says to run `claude update`. To check this offline against your installed CLI, with no credentials or network: `HARVEY_CLAUDE_CLI_TESTS=1 pytest tests/test_claude_cli_isolation.py`.

Your selected subscription covers model calls, subject to its usage limits. That lets Harvey use its skills library for personalization and reply handling without a separate model API bill.

**What this costs you:**

| | |
|---|---|
| Claude Pro/Max, ChatGPT with Codex, or Antigravity | your existing subscription |
| Finding businesses | **$0** on the default source, or ~$0.37 per 1,000 on the cheapest paid one |
| Reading their websites | **$0** — three HTTP requests each, no model call |
| Writing and sending | your subscription + a mailbox (~$7/mo Google Workspace) |
| Prospecting tools | **none.** No Apollo, no ZoomInfo, no Clearbit, no Clay. |

For comparison, the tools Harvey replaces start at $250–500/month — and they are weakest precisely where Harvey is strongest: small local businesses with 5–50 people, where single-provider contact coverage caps out around 30% and micro-businesses are frequently absent entirely.

**Harvey also budgets itself.** It reads the active provider's subscription quota and throttles to leave headroom for your own Claude or Codex work. Set `max_daily_claude_percent: 80` for either provider—the setting's name predates Codex support. If quota cannot be read, Harvey reports it as unavailable and falls back to its daily call budget.

Harvey runs locally with your own login. Check the terms and usage limits of the provider you choose.

---

## What Harvey actually does

Harvey runs a heartbeat: wake up, check the budget, decide what most needs doing, do it, log it, sleep. Every 15 minutes by default.

```
                 ┌──────────────────────────────────────┐
                 ↓                                      │
   Wake  →  Quiet hours?  →  Budget OK?  →  Decide  →  Act  →  Log  →  Sleep
                 │               │
              (sleep)         (sleep)
```

It decides deterministically, not by asking a model what to do — that would burn a call on something derivable from four counts:

```
handle replies  >  send campaigns  >  write campaigns  >  prospect  >  idle (analyze)
```

Profiling and outbox draining ride along on **every** cycle regardless, because they cost nothing and have to happen on schedule.

---

## Quick start

### Prerequisites

- **Python 3.11+** — check with `python3 --version`. macOS ships 3.9; if that's what you have, `brew install python@3.13` first.
- An active **Claude Pro or Max** subscription, with the CLI logged in (`claude login`) — or, opt-in, a **ChatGPT plan with the Codex CLI** ([see below](#using-a-chatgpt-plan-instead-codex))
- A mailbox to send from — Gmail/Workspace recommended, on a *dedicated secondary domain*. Microsoft 365 works too ([setup](docs/microsoft365.md))

### Easiest: let Claude set it up

```bash
git clone https://github.com/ethanplusai/harvey.git
cd harvey
claude
```

Then say: **"set up Harvey for me"**. The repo ships a `CLAUDE.md` that turns Claude Code into the setup wizard — it checks what state you're in, installs what's missing, asks what it needs, and trains Harvey on your product.

### Or do it yourself

```bash
# 1. Install — note python3.13, not python3. On macOS bare `python3` is the
#    system 3.9, and a venv built from it can't run Harvey.
python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .

# 2. Configure — every variable is documented inline with where to get it
cp .env.example .env && $EDITOR .env

# 3. Learn your product from your website (writes harvey.local.yaml; re-running
#    it updates the product, targeting and company and keeps your other settings)
harvey train https://your-company.com

# 4. Choose what makes a good prospect (see below — this one matters)
harvey signals --confirm free

# 5. Find businesses. Free source, no account needed.
harvey discover

# 6. Watch it work
harvey dashboard          # prints a private link to http://127.0.0.1:5555
harvey run
```

---

## You confirm what it looks for

This is the part that makes Harvey different from a black box, and it is deliberately not skippable.

Harvey knows how to collect **23 signals** about a business. It does not collect any of them until you say so. On first run it *proposes* the catalog; you confirm, skip, or reject each one in the dashboard or from the terminal:

```bash
harvey signals                       # review the catalog
harvey signals --confirm free        # everything that costs nothing (21 of them)
harvey signals --confirm SERP_RANK   # a paid one, opted into explicitly
```

Every signal shows what it is in plain language, **what it costs**, and how many companies already carry it.

| | Signal | What it tells you |
|---|---|---|
| **Discovery** | `SERP_RANK` | Where they rank for their own core search. Positions 11–30 are the sweet spot: visible enough to be trying, and losing. |
| | `NO_WEBSITE` | They have a listing and a phone and no site. If you sell websites, the highest-intent signal that exists. |
| | `UNCLAIMED_LISTING` | Nobody has claimed their Google Business Profile. |
| | `REVIEW_RATING` / `REVIEW_COUNT` | Good but invisible, or visible but struggling. |
| **Profile** | `INCUMBENT_AGENCY` | Who currently has the account, credited in their own footer. Every detection builds a roster you can target as a whole book of business. |
| | `RUNNING_GOOGLE_ADS` / `RUNNING_META_ADS` | They are spending on acquisition *today*. The strongest budget signal available for free. |
| | `NO_ONLINE_BOOKING` | Leads have to phone in, so after-hours demand is lost. |
| | `NO_SCHEMA_MARKUP` | A concrete, checkable SEO gap you can name in an email. |
| | `BLOG_STALE` | Content marketing started and abandoned. |
| | `SITE_PAGE_COUNT`, `TECH_STACK`, `BLOCKS_AI_CRAWLERS`, `HIRING_ROLE` | Size, platform, AI-search visibility, and what they're hiring for. |
| **People** | `CONTACT_FOUND`, `DECISION_MAKER_TITLE`, `LIKELY_OWNER`, `REGISTRY_VERIFIED` | Who decides, and whether they own the place. |
| **Contact** | `EMAIL_PATTERN`, `EMAIL_STATUS`, `CONTACT_FORM_URL` | Whether you can actually reach them. |

### Then a cohort is a query, not a list

Once signals are confirmed, the dashboard's cohort builder turns them into a target set — pick what a good prospect **must have** and what **disqualifies** them, and it counts the matches live:

```
Has a marketing agency  AND  running Google Ads  AND NOT  already has online booking
→ 23 companies
```

That is a campaign with a reason behind it. You know exactly why each of those 23 is on the list, and so does the email Harvey writes them.

---

## The prospecting pipeline

Four stages. Each one runs independently, re-runs safely, and states its cost.

### 1. DISCOVER — who exists

The only stage that spends money, so it always estimates first and never calls anything until you press go.

```bash
harvey discover --providers   # the menu, with real prices
harvey discover --estimate    # projected spend, then exits
harvey discover               # go
```

| Source | Cost | Free tier | Best for |
|---|---|---|---|
| **OpenStreetMap** *(default)* | free | unlimited, no account | Trying the whole pipeline before paying anyone. Coverage is thin for businesses without a storefront: OSM maps premises, so whole trades are barely present (under 2,000 roofers and under 2,300 plumbers in the entire US). Harvey says so in the UI rather than quietly under-delivering. |
| **DataForSEO Business Listings** | $0.372 / 1,000 | $1 credit | Local trades, clinics, contractors. Phone, domain, rating, claimed status — and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.0018 / search | $1 credit + free sandbox | Rank as the buying signal. |
| **Serper** | ~$0.30–1.00 / 1,000 | 2,500 free, no card | The easiest paid one to try. |

Cities geocode themselves (cached forever, one lookup each). Directories, aggregators and institutions are filtered by pattern, not a blocklist — `en.wikipedia.org` and `biz.yelp.com` never make it in.

> **Depth 20–30, not 100.** Google removed 100-results-per-page in September 2025, so depth 100 is now billed as ten pages nearly everywhere. Below rank 30 it is mostly directories anyway.

### 2. PROFILE — what they are

**Free.** Three HTTP requests per business. No browser, no model call. This is the highest value-per-effort stage in the whole system, because it produces the facts that make an email specific.

Website requests from Scout, Profile, and the local training crawler are limited
to public HTTP(S) destinations. Every DNS answer and redirect is checked; local,
private, link-local, and metadata addresses are rejected. Connections use a
validated IP while preserving the website's TLS identity. These website clients
ignore environment proxies, so proxy routing cannot bypass that boundary.

Homepage, `robots.txt`, `sitemap.xml`, plus the team and careers pages when they exist — then regex over HTML already in hand. Out comes the incumbent agency, the ad pixels, the missing schema, the abandoned blog, the open roles, the named humans.

Agency detection is the compounding one. Almost every agency credits itself in the footer, and Harvey scores the wording: *"Powered by"* / *"Website by"* → 0.9 confidence; a bare descriptive link → 0.75; an unlabelled link → 0.35 and **not recorded**. A wrong incumbent in an email is worse than saying nothing.

### 3. ENRICH — who decides

Free public registries first. Name plus title from the team page, confirmed against an authoritative registry where one exists — which also filters out your own parsing artifacts, because a "person" who matches nothing usually wasn't one.

When a company's site names nobody and Harvey has no contact there yet — common for small trades — it falls back to a general address the company prints on its own homepage or contact page (only general mailboxes such as `owner@`, `office@`, `info@` or `hello@`, never a person's own address). It is never guessed, it is verified like any other address, and those emails greet the company's team instead of a first name.

### 4. VERIFY — can you reach them

Harvey learns each company's **address pattern** and verifies one candidate, rather than brute-forcing name variations. Raw SMTP probing is not viable from a laptop — outbound port 25 is usually blocked, and Google Workspace and Microsoft 365 accept everything from an unknown IP.

Every address is tagged honestly: `verified` / `risky` (catch-all) / `guess` / `invalid`. **Only deliverable ones are ever sent to.** A guess is never treated as a win.

> Harvey is worth running even if you never let it send. `harvey export` writes a sequencer-ready CSV of everything it found.

---

## The skills library

Harvey's sales knowledge lives in `skills/` as **plain Markdown you can edit**. There is no fine-tuning, no vector store, no retrieval step. Before an agent runs, the skills it needs are concatenated straight into its prompt. Change a file, and the next heartbeat behaves differently — no restart, no code change.

This is the main way you shape Harvey. If its emails are too pushy, edit `email_frameworks.md`. If it mishandles a specific objection, edit `objection_handling.md` and add the response you'd actually give.

### What ships

**`email_frameworks.md`** — 301 lines, the most important file in the repo. Opens with the only rule that matters: *write like a real person sending a real email.* Contains a hard ban list on AI tells (no "I hope this finds you well", no "I wanted to reach out", no "circling back"), compliance rules that override every style rule beneath them, 2026 reply-rate data on length and cadence and why link tracking hurts you, and five frameworks — **AIDA, PAS, BAB, QVC, 3Ps** — with selection rules for when each one fits.

**`signal_playbook.md`** — How to turn a signal into the first line of an email. Which signal wins when a company has several. How to reference something you observed without sounding like you ran a scan on them.

**`objection_handling.md`** — The **LAARC** loop (Listen → Acknowledge → Assess → Respond → Confirm) with worked responses across the four objection families: budget, authority, need, timing. The framing throughout is that you are not "overcoming" an objection, you are finding out what the actual concern is.

**`lead_qualification.md`** — **BANT** for the first screen, ICP scoring 1–10 for prioritization, **MEDDIC** for anything complex. Includes explicit disqualification criteria, which matter more than the qualification ones.

**`sales_methodology.md`** — The operating philosophy: the ABC loop, how a conversation should flow across stages, tone calibration, and the ethical lines Harvey does not cross.

**`offer_strategy.md`** — The offer ladder by engagement level, and the rule that governs it: *never lead with an offer, never pitch in a cold email.* First contact exists to start a conversation. Offers come out only after genuine interest.

**`prospecting_tactics.md`** — Search operators, company-website mining, trigger events, referral paths. Finding people without paying for a database.

**`account_navigation.md`** — Companies versus contacts, multi-threading rules, which door to knock on first when a company has several.

**`linkedin_outreach.md`** — Connection sequences and rate limits, opening with the risk section it should open with: automating LinkedIn violates their ToS, never use a fake identity, and if someone asks whether this is automated, say yes immediately.

**Generated for your product by `harvey train <url>`:**

- **`product_knowledge.md`** — what you sell, the benefits, pricing, use cases, the pain you solve, buying triggers
- **`competitive_intel.md`** — battle cards per competitor, differentiation angles, migration paths

*(Both are gitignored. Your positioning and pricing shouldn't land in a public repo by accident.)*

### Which agent gets which

Defined in `harvey/brain.py` — edit the map to change it.

| Skill | Scout | Writer | Handler | Sender | LinkedIn |
|---|:-:|:-:|:-:|:-:|:-:|
| `prospecting_tactics` | ● | | | | ● |
| `lead_qualification` | ● | | | | |
| `account_navigation` | ● | | | | |
| `signal_playbook` | ● | ● | | | |
| `email_frameworks` | | ● | | ● | |
| `sales_methodology` | | ● | ● | | |
| `offer_strategy` | | ● | ● | | |
| `objection_handling` | | | ● | | |
| `linkedin_outreach` | | | | | ● |
| `product_knowledge` | ● | ● | ● | ● | ● |
| `competitive_intel` | | ● | ● | | |

Adding a skill: write the file, add its name to `skill_map` in `harvey/brain.py`. That's the whole process.

---

## The sub-agents

Harvey feels like one agent. Underneath it's five, each loaded with different skills.

| Agent | What it does |
|---|---|
| **Scout** | Scores and personalizes what the collectors found. Python does the searching and the email resolution; the selected model is used only where judgment is actually needed — which is what keeps the token cost sane. |
| **Writer** | Generates 3-email sequences. Email 1 under 75 words, email 2 under 75, email 3 under 40. Hard ban list on AI-sounding language. |
| **Sender** | Renders merge variables per prospect and stages each email into the outbox with a send time. Drains due and approved items with human-like pacing, enforcing the daily cap and stop-on-reply. |
| **Handler** | Polls for replies, dedups them, classifies intent, advances the conversation stage, and queues responses through the same approval ladder. Detects bounces, marks the address invalid, cancels that prospect's queued sends, and trips a global kill switch past a bounce-rate threshold. |
| **Analyst** | Runs on idle cycles. Pipeline stats, campaign performance, intent distribution, what's actually working. |

Conversation stages: `initial_outreach → engaged → qualifying → presenting → negotiating → closing → closed_won / closed_lost`

---

## Nothing sends without you

By default **every outgoing email waits for your approval.** Harvey is a copilot until you decide otherwise.

The dashboard's Outbox is a decisions desk: one email fills the pane, the rest wait in a rail, and you work the queue with `A` approve, `R` reject, `J`/`K` to move. That shape is deliberate — a wall of stacked drafts invites a single approve-all reflex, which is exactly the review the approval ladder exists to force.

```bash
harvey outbox                 # review from the terminal
harvey outbox --approve-all
harvey sending pause          # kill switch, stops everything mid-flight
```

Before anything leaves, a **deterministic pre-send gate** — no model involved — rejects: unrendered merge tags, banned phrases, emails over the length cap, too many links, HTML bodies, non-deliverable addresses, and any mismatch between the recipient and the database record.

Optionally, the same step can ask [Precleared](https://precleared.dev) whether cold email to each new address is allowed before the first one goes out. See [Legal and deliverability](#legal-and-deliverability).

When you trust it, set `channels.email.require_approval: false` for full autopilot.

**Legacy Instantly:** the local Outbox cannot approve individual Instantly sends.
With `require_approval: true`, Harvey holds campaign deployment (including
activation retries) and routes replies to a human. Choose Gmail, Microsoft 365,
or SMTP for the approval queue. Set `require_approval: false` explicitly to use
Instantly's automatic sending. This does not pause campaigns already running
on Instantly; manage those in Instantly itself.

---

## The dashboard

```bash
harvey dashboard     # prints the link to open, e.g. http://127.0.0.1:5555/#token=…
```

**Opening it.** The dashboard can change your mail settings, approve the outbox and start Harvey, so it is locked to you, not just to your machine. On first start it creates a random token in `data/dashboard-token` (readable only by you) and prints a link carrying it after the `#`. Open that link: the page keeps the token for that browser tab only and removes it from the address bar. A new tab, or a tab opened without the link, shows a locked screen; paste the token there, or run `harvey dashboard --url` to print the link again. Every API request needs the token as an `Authorization: Bearer` header — scripts and `curl` included — and the token is never accepted from a cookie or a query string. Delete `data/dashboard-token` and restart the dashboard to replace it.

**It only listens on this machine.** `--host` accepts loopback addresses only (`127.0.0.1`, `::1`, `localhost`), and requests must name one of them in their `Host` header, which also stops a DNS name pointed at 127.0.0.1 from reaching it. Requests sent by other websites open in your browser are refused. To use it from another computer, tunnel instead of exposing it: `ssh -L 5555:127.0.0.1:5555 your-machine`, then open the printed link locally. The token protects against other users and other websites, not against programs already running as your own user account, which can read the token file just as they can read `.env` or the database.

**Windows:** the token file is protected by the permissions of the folder it lives in (your user profile normally restricts this to you); Harvey doesn't apply POSIX file modes there.

Plain HTML, CSS and JavaScript served from `harvey/web/`. No build step, no framework, no bundler — edit `app.css` and reload. Fonts are vendored, so it renders correctly with the network off.

- **Today** — an asymmetric two-column screen: anything waiting on a decision down the left with the activity feed beneath it, and a rail carrying pipeline figures, collector runs and setup. It opens here rather than on a setup checklist, because the question you actually have is "is anything waiting on me?" Setup lives in the rail and disappears entirely once it's done.
- **Signals** — the confirmation gate and the cohort builder
- **Discover** — the provider menu with prices side by side, an estimate, then a run
- **Companies / Contacts** — everything found, with CSV export
- **Outbox** — the decisions desk
- **Conversations** — every reply and how Harvey handled it
- **Usage** — Claude or Codex quota gauges and per-agent token counts. No dollar figures: you're on a subscription, you aren't billed per token, and pretending otherwise would be theater.

Three rules hold the interface together:

**The interface carries no colour.** The chrome is warm greyscale in both modes, so the only coloured thing on screen is status — if something has a hue, it means something.

**One status vocabulary.** Prospects, campaigns, conversations, the outbox, signals and email deliverability all resolve to the same five tones, so "waiting on you" looks identical wherever it appears.

**Every figure is monospaced and tabular**, so columns of numbers line up and a value changing doesn't shift the layout.

Appearance has three states, not two. `auto` follows your OS and is the default, so the dashboard matches the rest of your machine until you deliberately pick light or dark. Dark isn't a separate design — it's the same design re-tokenised.

---

## How the data is stored

SQLite at `data/harvey.db`. One idea drives the schema:

> **Every fact is a row, never a column.**

Not `companies.has_agency`. Not `companies.rank`. A row in `observations`:

```
observations(company_id, prospect_id, collector, signal_code,
             value_num, value_text, confidence, evidence_url, observed_at, run_id)
```

Four things fall out of that, none of which you get from columns:

1. **A new signal needs no migration.** A collector invents a code and it works.
2. **History is free.** The same signal observed quarterly *is* a time series — and "they dropped their agency last quarter" is a far better trigger than any static fact.
3. **Confidence and provenance travel with the fact** instead of being lost on overwrite. *"We think their agency is X, 0.9 confidence, here's the URL."*
4. **A cohort is a query.** Set intersection happens in SQL, not in application code over a capped fetch.

The vocabulary is governed: a database trigger rejects any `signal_code` not in the `signal_codes` table, so a typo fails loudly instead of quietly inventing a junk signal.

Other tables: `companies`, `prospects`, `campaigns`, `conversations`, `outbox`, `actions`, `runs`, `usage_events`, `email_patterns`, `settings`, `feedback`.

---

## Configuration

Two files, both plain text.

**`.env`** — credentials. Every variable documented inline with where to get it. The only required one is a mail provider; everything else is optional.

**`harvey.yaml`** — who Harvey is and who it sells to:

```yaml
persona:      { name, company, role, email, tone }
product:      { name, description, pricing, key_benefits, objection_responses, offer }
icp:          { industries, titles, company_size, geography, hiring_signals, geo_coordinates }
channels:
  email:      { provider: gmail | microsoft | smtp | instantly, max_daily_sends, require_approval,
                send_to_risky, max_bounce_rate }
usage:        { max_daily_claude_percent, heartbeat_interval_minutes, quiet_hours }
brain:        { provider: claude | codex, model, reasoning_effort }   # optional; claude by default
writer:       { humanizer }   # optional; true adds one editing pass per batch using ~/.claude/skills/humanizer/SKILL.md
```

`max_daily_claude_percent` caps whichever subscription `brain.provider` uses (the name predates Codex support).

If `harvey.local.yaml` exists it wins. It's gitignored, so a fork can carry real product configuration while the tracked `harvey.yaml` stays a template — nobody publishes their positioning and pricing by accident.

### Commands

```bash
harvey run                   # the heartbeat loop
harvey dashboard             # web UI at 127.0.0.1:5555 (--url: print the unlock link)
harvey signals               # review/confirm what to prospect against
harvey discover              # find businesses; --providers / --estimate
harvey profile               # read discovered companies' sites (free)
harvey train <url>           # learn a product from its website (--provider codex --model gpt-5.5)
harvey status                # pipeline summary
harvey usage                 # active provider's quota + per-agent tokens
harvey outbox                # review queued email
harvey export                # deliverable prospects → CSV
harvey sending pause|resume  # kill switch
harvey gmail auth            # one-time Gmail OAuth
harvey microsoft auth|test  # one-time Microsoft 365 sign-in (docs/microsoft365.md)
```


## Using a ChatGPT plan instead (Codex)

Claude is the default and needs no change. If you'd rather spend a ChatGPT plan (Plus, Pro, Business, …), Harvey can think through the [Codex CLI](https://developers.openai.com/codex) instead — still a subscription, never per-token API billing.

```bash
npm install -g @openai/codex@0.159.3   # the Codex version Harvey has verified (see Requirements)
codex login                            # choose "Sign in with ChatGPT" — API-key logins are refused
```

Then either pick **codex** in `harvey setup` (it suggests `gpt-5.5`), run `harvey train <url> --provider codex --model gpt-5.5`, or add to your config:

```yaml
brain:
  provider: codex
  model: gpt-5.5         # or an available Luna, Sol, or Terra model ID
  reasoning_effort: ""   # optional: minimal | low | medium | high | xhigh
```

What to know:

- **It's one or the other.** Harvey uses exactly the provider you configure. If Codex fails, the call fails with a clear log message; it never falls back to Claude (or to an API key). A Codex-only machine doesn't need Claude installed.
- **Your Codex setup is left alone.** Each call runs throwaway `codex` processes in a private temporary home and empty workspace, so your `~/.codex/config.toml`, MCP servers, plugins, hooks, skills, `AGENTS.md` and memories are never loaded, and Harvey's model choice never changes your own Codex default.
- **No tools, verified before every prompt.** Prompts contain untrusted text (prospect replies, scraped pages), so the model must have nothing to act with. Harvey disables every tool-providing Codex feature, web search, sub-agents, hooks, skills and project instructions, starts the thread with no execution environment, and then checks Codex's own reports (effective config, account type, thread settings, MCP status, hooks) before sending anything. Anything it can't confirm stops the call. During the turn, any attempted tool call, approval request or non-text item kills the runtime. A read-only sandbox is also set, as extra protection only.
- **Your login only goes to OpenAI.** Before your login is put into the runtime at all, Harvey starts Codex without it and checks that requests would go to the built-in OpenAI provider and the default ChatGPT endpoint. Any custom provider, overridden built-in provider, base URL, telemetry exporter or other endpoint in any Codex config layer (for example `/etc/codex/config.toml` or a managed config) stops the call there.
- **Login handling.** Harvey supports Codex's file credential storage (`~/.codex/auth.json`, or `$CODEX_HOME/auth.json`, mode `600`): the default unless you've configured an OS keyring. Keyring storage isn't supported. On every call Harvey reads that file (read-only, never writing or moving it) and gives the runtime a private copy *without the refresh token*, deleted after the call. Codex therefore can't change or renew your real login. When the login has expired, or is due for Codex's routine renewal (after about 8 days), Codex calls stop with "Your Codex login has expired or is due for renewal". Use Codex once yourself (it renews the login automatically) or run `codex login`, and Harvey picks up the renewed login on its next call. Harvey never renews it for you.
- **Models.** Set `brain.model` to an exact model ID from your account's catalog, including Luna, Sol, and Terra models such as `gpt-5.6-luna`, `gpt-6.1-sol`, and `gpt-5.6-terra`. `gpt-5.5` remains supported when present in your account's catalog and is the setup suggestion. Harvey creates a private catalog copy that preserves model capabilities while selecting direct tool mode and removing experimental tools. The model receives no tools under Harvey's lockdown. A blank `model` selects the highest-priority listed model that can use this configuration; hidden models require an explicit ID. An explicit model is used exactly or the call fails. Unknown model IDs and unrecognized tool modes are rejected. Availability depends on your ChatGPT account.
- **Quota.** The budget gate and dashboard read your ChatGPT plan's real rate-limit windows from Codex, for the limit bucket that applies to your model, and compare them to `usage.max_daily_claude_percent`. If Codex reports that usage is blocked, or a call fails on the plan's usage limit, Harvey treats the plan as full (even when no window is reported) and stops Codex calls for an hour. If the limits can't be read, the dashboard and `harvey usage` say "Quota unavailable" and the budget falls back to Harvey's own daily call count. Usage is recorded per provider; Codex calls are logged with tokens but no dollar cost, because the plan isn't billed per token.
- **When Codex can't be used, Harvey pauses rather than guessing.** Before each heartbeat, Harvey checks everything a call needs except the prompt itself: the Codex CLI version, the isolation, your login (expired or due for renewal), your ChatGPT account, and that your configured model can run with no tools. No prompt is sent, so nothing is used from your plan beyond a quick account check. If something is wrong, the whole cycle is skipped: no reply is read or marked handled, nothing is drafted or sent. The log line, `harvey status`, and the dashboard's Actions card say "Codex paused" and why. If Codex stops working part-way through a cycle (for example the plan hits its usage limit), the reply being handled is left exactly as it was for a clean retry, not escalated, half-recorded or lost, and that cycle sends nothing. Fix the cause (usually `codex login`), and Harvey resumes on its next heartbeat: a renewed login or reinstalled CLI is noticed immediately, a usage-limit pause ends after an hour, and other fixes are picked up within 30 minutes. `harvey status --check` runs the same check on demand and exits non-zero if it fails. Brief failures (a crash or timeout) just skip one cycle. Harvey never falls back to Claude or an API key.
- **Requirements.** **macOS or Linux**, Codex CLI **0.159.3** exactly, and Node.js for its npm package. **Windows isn't supported for Codex:** Harvey refuses it there before reading your login; use the Claude provider on Windows. Harvey checks the Codex version before every call and refuses any other version, including newer ones, because a different Codex can add tools Harvey's lockdown doesn't know about. Support for a new version comes with a Harvey update. Live use with a real ChatGPT account has been verified on macOS and ARM64 Linux. The offline suites also passed on x86-64 Linux under CPU emulation; CI covers Linux and macOS. The Docker image ships the Claude CLI only, so Codex isn't supported in Docker. To re-verify the lockdown offline against your installed CLI, with no credentials or network: `HARVEY_CODEX_CLI_TESTS=1 pytest tests/test_codex_cli_surface.py`.
- **Overhead.** Each call checks the CLI version, starts Codex once without your login to verify its configuration, reads the model catalog, then starts it again to run the prompt; each heartbeat's check does the same without the prompt. That adds a few seconds per call, and one Brain's Codex calls run one at a time.

---

## Using an Antigravity subscription (agy)

If you have an Antigravity subscription (e.g. Google One AI Premium, Gemini Advanced, or Google Workspace), Harvey can run through the Antigravity CLI (`agy`) in headless print mode.

Set `brain.provider` in `harvey.yaml`:

```yaml
brain:
  provider: antigravity
  model: gemini-3.8-flash-high  # or claude-sonnet-4-6 (optional)
  reasoning_effort: medium       # low | medium | high | xhigh | max (optional)
```

- **Requirements:** Install the `agy` CLI from [https://antigravity.google](https://antigravity.google) and authenticate once in your terminal.
- **Zero tools & security:** Prompts are executed with `--disable-slash-commands` and tool execution omitted to protect against prompt injection from scraped web data or inbound emails.
- **Long prompt handling:** Prompts exceeding argument length limits are passed cleanly through stdin (`--input-format text`).
- **Quota & billing:** Model calls run on your Antigravity plan quota without per-token API charges.

---

## Legal and deliverability

Harvey automates outreach, but **you are the sender.** Cold email is legal in most places when done right and expensive when done wrong — CAN-SPAM penalties run to $53,088 per email. Harvey ships with compliant defaults. Keep them.

**CAN-SPAM (US).** Truthful subject line and accurate from-address — Harvey's copywriting rules forbid fake "re:" threads and impersonation. A working opt-out, honored fast: Harvey treats any opt-out wording as immediate and permanent. Your physical mailing address in the footer — configure this before your first campaign.

**GDPR / PECR (EU & UK).** B2B cold email needs a defensible legitimate interest: the pitch must be genuinely relevant to that person's role, you must know where the data came from, and objecting must be effortless. If you can't say why a specific person would care, Harvey shouldn't email them — and its qualification rules say so.

**Per-recipient check (optional).** Those rules turn on facts about each recipient: a UK sole trader needs consent where a limited company doesn't, Germany wants prior consent even for business email, and Canada's CASL depends on whether the address was published. With `compliance.precleared.enabled: true`, Harvey asks [Precleared](https://precleared.dev) about each new recipient before the first cold email. Anything other than `ok_to_email` holds that email back, with the rule and the next step shown in the Outbox, and the signed receipt id is logged with every send. One check per address, cached for 30 days (changing the sender country, address source, product description or prospect country triggers a fresh check); replies are never checked; native providers (Gmail, SMTP) only. Precleared works out each recipient's country from the address, company records or the company's website; if all your prospects are in one country, also set `prospect_country` (for example `US`), which it uses only when none of those show it. Precleared receives the recipient's address, your sender country, the address source, your product description and, if set, your prospects' country. Without a key it allows 10 checks a day, and a free key (`PRECLEARED_API_KEY`) allows 25. Put the setting in `harvey.yaml`, or in `harvey.local.yaml` if you trained Harvey, since that file wins.

**Bot disclosure.** Some jurisdictions require disclosing automation. Harvey is instructed to answer truthfully, always, if a prospect asks whether they're talking to an AI. Never configure it otherwise.

**LinkedIn.** Browser automation violates LinkedIn's ToS and can get the account restricted. Off by default. If you turn it on, use an account you can afford to lose.

LinkedIn requires both `channels.linkedin.enabled: true` and configured
credentials. Training a product does not enable it; the setup wizard records
your explicit choice.

**Deliverability — warm up or burn out.** Sending cold email from your main domain, or at volume on day one, lands you in spam permanently.

1. Buy a **dedicated sending domain** (`getacme.com`, not `acme.com`).
2. Set up **SPF, DKIM and DMARC** on it. Without all three, Gmail junks you.
3. **Warm up for 2–4 weeks** before real volume.
4. **Ramp slowly** — 10–20/day per inbox, adding ~5/day. The `max_daily_sends: 50` default is a ceiling, not a target.
5. **Watch bounces.** Above ~3%, stop and fix list quality. Harvey trips its own kill switch past your configured threshold.

*None of this is legal advice. Sending at scale or into regulated industries? Talk to a lawyer.*

---

## Troubleshooting

**`command not found: harvey`** — `source .venv/bin/activate` first.

**`ModuleNotFoundError: No module named 'harvey'` after install (macOS)** — Python 3.13 silently ignores `.pth` files carrying the macOS hidden flag, and some Macs propagate that flag into `.venv`. Run `harvey install` again (it auto-fixes), or: `ln -s "$(pwd)/harvey" .venv/lib/python3.13/site-packages/harvey`

**`externally-managed-environment`** — Use a venv, not system Python.

**`SyntaxError` or odd `ImportError`s right after install (macOS)** — Your venv is Python 3.9. `python3 -m venv` builds from `/usr/bin/python3`, which macOS keeps at 3.9. Check with `.venv/bin/python -V`, then rebuild: `rm -rf .venv && python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .`

**Claude headless mode fails** — `claude login`, and confirm the subscription is active. Test with `claude -p "say hi"`. In Docker, mount `~/.claude` into the container (writable: the CLI refreshes its login there). If the log says the Claude CLI "lacks --safe-mode" (or another flag), run `claude update` (in Docker, rebuild the image); Harvey resumes on its own. If your Claude login depends on `env` or `apiKeyHelper` entries in your Claude settings files, set those variables in Harvey's environment instead: Harvey's calls don't read settings files. `harvey status --check` confirms the CLI without sending a prompt.

**Dashboard says "locked"** — Open the link `harvey dashboard` printed (or `harvey dashboard --url`). If it says the token file can't be used, follow the message: usually delete `data/dashboard-token` and restart the dashboard. Visit it as `127.0.0.1` or `localhost`; other host names are refused.

**Codex calls fail** — Read the log line; every Codex error says what to do. "Not logged in" or "expired or is due for renewal" → use Codex once or run `codex login` with ChatGPT. "Hasn't been verified with Harvey" → install the supported Codex version named in the message. "Isolation could not be verified" → a Codex config layer outside your `~/.codex` (for example a managed or `/etc/codex` config) forces a tool, hook, MCP server, instructions or custom endpoint back on; Harvey refuses to send prompts until it's gone. Model catalog errors → choose an available model ID from the catalog shown in the message, or leave `brain.model` blank to select a listed model. Harvey never switches to Claude on its own.

**Discovery finds nothing** — Confirm signals first (`harvey signals --confirm free`); nothing is collected until you do. If you're on the free OpenStreetMap source, it only covers mapped trades and is thin for service-area businesses — `harvey discover --providers` shows the alternatives.

**Overpass is throttling** — It's free volunteer infrastructure. Harvey walks three mirrors before giving up. Wait a few minutes, or use a paid provider for bulk work.

**Emails land in spam** — Almost always the domain, not the copy. Check SPF/DKIM/DMARC, confirm warmup ran, halve your volume.

**Harvey does nothing during the day** — Check `quiet_hours` and whether it hit `max_daily_claude_percent`. `harvey status` shows current state; the `actions` table logs every decision.

**How do I stop it right now?** — `Ctrl+C`, or `harvey sending pause` to stop outbound while leaving the loop running. State is in SQLite, so it resumes cleanly.

**Where does my data live?** — All local: `data/harvey.db`, `.env`, `harvey.yaml`. Nothing goes anywhere except the APIs you configured.

---

## Project structure

```
harvey/
├── main.py              # heartbeat loop
├── brain.py             # Claude/Codex provider selection + skills + usage
├── claude_runtime.py    # Isolated Claude CLI calls (safe mode, no tools, clean dir)
├── codex.py             # Isolated Codex subscription runtime
├── state.py             # SQLite schema, migrations, observations, cohorts
├── signals.py           # the 23-signal catalog Harvey proposes
├── pipeline.py          # DISCOVER → PROFILE chaining
├── gate.py              # deterministic pre-send checks
├── dashboard.py         # FastAPI JSON API
├── dashboard_auth.py    # dashboard token, Host/Origin checks
├── collectors/
│   ├── discover.py      # provider adapters, junk filtering, entity resolution
│   └── profile.py       # the free HTTP profiler
├── agents/              # scout, writer, sender, handler, analyst
├── integrations/        # mail providers, email finder, quota
└── web/                 # dashboard HTML/CSS/JS — no build step

skills/                  # ← the sales knowledge. Editable Markdown.
prompts/                 # ← agent system prompts. Also editable Markdown.
tests/                   # 242 tests
```

---

## Philosophy

**Autonomous doesn't mean unsupervised.** Quiet hours, spend caps, send limits, an approval queue, a deterministic pre-send gate, and a kill switch. Harvey asks before it spends and before it sends, until you tell it not to.

**You decide what a good prospect is.** Harvey proposes signals; you confirm them. A prospect list you can't explain is a prospect list you shouldn't send to.

**If it can be gotten deterministically, don't use a model.** Rank position, tech detection, junk filtering, the priority decision, the pre-send gate — all plain code. Claude or Codex is reserved for the parts that need judgment. That's why this runs on a subscription at all.

**A failure is an observation, not silence.** "No team page found" is a finding. "Overpass is throttling" is a finding. Silent failures look exactly like clean results, which is how a broken run gets mistaken for an empty market.

**The database is the asset, not the agent.** The initial list is worth little. The same signals observed over quarters — who changed agencies, who started spending on ads, who let their content go stale — is worth a great deal.

**Everything is editable.** Prompts, skills, config, the dashboard. All plain text. You don't need to be a developer to change how Harvey sells.

---

## Roadmap

**Done:** heartbeat loop · observation data model with governed vocabulary · user-confirmed signal catalog · cohort builder · discovery provider menu with cost estimates · free HTTP profiler with agency detection · pattern-first email finding with honest status · native mail providers (Gmail API, Microsoft 365 via Graph, SMTP+IMAP) · outbox approval ladder · deterministic pre-send gate · bounce detection and kill switch · reply handling with intent classification · subscription quota tracking · website trainer · web dashboard · CSV export

**Next:** people enrichment from public registries · consent-gated voice callbacks · calendar integration for auto-booking · scheduled re-observation so signal *changes* trigger outreach · multi-product support

> **On voice:** a fully autonomous AI cold dialer is not on this roadmap, and won't be. Under FCC 24-17 an AI-generated voice is an "artificial voice" under the TCPA, and 47 CFR 64.1200(a)(1) has no B2B exemption — penalties run $500–1,500 per call, uncapped, and some states ban it outright. Voice here will be **consent-gated**: inbound and opt-in-triggered callbacks only.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Current release: **0.2.1**.

---

## License

MIT

---

*"Put that coffee down. Coffee is for closers."* — Blake, *Glengarry Glen Ross*
