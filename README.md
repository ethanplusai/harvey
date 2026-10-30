# Harvey

**An autonomous sales agent that runs on your Claude Code or Codex subscription.**

Harvey finds businesses worth selling to, learns something specific about each one, writes cold email that references it, sends it, reads the replies, and works the conversation toward a meeting. It runs on your machine, in a loop, on its own.

The unusual part: **there is no model API bill.** Harvey uses your Claude Pro or Max subscription through Claude Code, or your ChatGPT subscription through Codex. Model calls use the plan you already pay for, without an API key or per-token API billing. Claude is the default; [Codex is an opt-in provider on macOS and Linux](#using-a-chatgpt-plan-instead-codex).

```
$ harvey run

============================================================
Harvey is online. Always Be Closing.
============================================================
Checking pipeline state...
Decision: handle_replies (2 new replies)
  handler   → Elena Park, Harbor Dental Studio     interested, answer drafted
  handler   → Priya Nair, Highline Roofing         objection, answer drafted
Decision: write_campaign (3 new prospects with no drafts)
  writer    → 2-email sequence: Phoenix HVAC hiring a dispatcher
  outbox    → 5 emails waiting for your approval
Cycle complete. Sleeping for 15 minutes.
```

And the dashboard it keeps, at `localhost:5555`. In this order: the Today screen, a
conversation Harvey handed back because a prospect asked for a contract, approving
a drafted reply from the outbox, confirming a signal, building a cohort from
confirmed signals, searching contacts, and the same screen in dark mode:

![Harvey's dashboard: the Today screen, a conversation handed back for a human, approving a reply in the outbox, confirming a signal and building a cohort, searching contacts, then dark mode](docs/dashboard.gif)

<sub>The demo data is invented: "Bookwell" selling online booking to roofers, dental practices and HVAC shops, on `.test` domains.</sub>

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
| Claude Pro/Max or a ChatGPT plan with Codex access | your existing subscription |
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
- A warmup service on that mailbox — we recommend [TrulyInbox](https://trulyinbox.com/?via=harvey). It warms a new mailbox before cold email, keeps it out of spam after, and Harvey reads its inbox rate to tell you when you're ready ([Warmup](#warmup-with-trulyinbox))

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

### Or deploy with Docker (Linux)

Docker Compose runs the dashboard; start and stop the heartbeat from its
Actions card. Host networking preserves the dashboard's loopback-only bind at
`127.0.0.1:5555`. This Compose setup supports native Linux; use the pip install
above on macOS or Windows. On a remote Linux host, reach the dashboard through
an SSH tunnel rather than exposing its port to the network.

```bash
git clone https://github.com/ethanplusai/harvey.git
cd harvey

# Create the credentials file only if you do not already have one.
test -e .env || cp .env.example .env

# Run as your Linux user, never root. Set these again when rebuilding.
export HARVEY_UID=$(id -u) HARVEY_GID=$(id -g)

# Sign in to Claude on the host first; ~/.claude must already exist.
# The image ships Claude only; Codex is not included.
docker compose build
docker compose run --rm harvey setup

docker compose up -d
docker compose logs harvey
# Open the private unlock link printed in the logs.
```

Compose mounts the checkout directory at `/app`. Setup credentials, both YAML
config files, training output, knowledge files, and `data/` remain in that
checkout when containers are recreated. Directory mounting also permits atomic
configuration saves; do not replace it with individual config-file mounts.
Harvey loads `.env` directly. Restart the container after editing credentials.
The checkout and Claude auth directory must be writable by the selected user.

To train later, use `docker compose run --rm harvey train <product-url>`;
`harvey.local.yaml` is saved directly to the checkout. Back up this file, `.env`,
`skills/`, and `data/` before moving installations. The running code also comes
from the checkout: restart after updating it, and rebuild when dependencies or
the Dockerfile change. The build context excludes local data and credentials.

The dashboard listens on 5555. If you change the port in the Compose `command`,
change the Dockerfile health-check port too and rebuild. Stop the service with
`docker compose down`; its files remain in the checkout. Use a host install if
you need the Codex provider.

---

## You confirm what it looks for

This is the part that makes Harvey different from a black box, and it is deliberately not skippable.

Harvey knows how to collect **29 signals** about a business. It does not collect any of them until you say so. On first run it *proposes* the catalog; you confirm, skip, or reject each one in the dashboard or from the terminal:

```bash
harvey signals                       # review the catalog
harvey signals --confirm free        # everything that costs nothing (26 of them)
harvey signals --confirm SERP_RANK   # a paid one, opted into explicitly
```

Every signal shows what it is in plain language, **what it costs**, and how many companies already carry it.

| | Signal | What it tells you |
|---|---|---|
| **Discovery** | `SERP_RANK` | Where they rank for their own core search, stored with the search phrase. Positions 11–30 are the sweet spot: visible enough to be trying, and losing. |
| | `IN_LOCAL_PACK` / `MAPS_RANK` | Whether they hold one of the three map slots above the results, and where they sit in Google Maps. |
| | `NO_WEBSITE` | They have a listing and a phone and no site. If you sell websites, the highest-intent signal that exists. |
| | `UNCLAIMED_LISTING` | Nobody has claimed their Google Business Profile. |
| | `REVIEW_RATING` / `REVIEW_COUNT` | Good but invisible, or visible but struggling. |
| **Profile** | `INCUMBENT_AGENCY` | Who currently has the account, credited in their own footer. Every detection builds a roster you can target as a whole book of business. |
| | `RUNNING_GOOGLE_ADS` / `RUNNING_META_ADS` | They are spending on acquisition *today*. The strongest budget signal available for free. |
| | `NO_ONLINE_BOOKING` | Leads have to phone in, so after-hours demand is lost. |
| | `NO_SCHEMA_MARKUP` | A concrete, checkable SEO gap you can name in an email. |
| | `BLOG_STALE` | Content marketing started and abandoned. |
| | `SITE_PAGE_COUNT`, `TECH_STACK`, `BLOCKS_AI_CRAWLERS` | Size, platform, AI-search visibility. |
| | `HIRING_ROLE` / `OPEN_ROLES` | What they're hiring for, and how many roles are open. When their site links a Greenhouse, Lever, Ashby or Workable board, Harvey reads that board's public feed (one request): roles matching `icp.hiring_signals` (else `icp.titles`), with department, location, posting date and the posting's URL; when the board lists none, it records "not hiring" so a closed role stops counting. Otherwise it reads the careers page. Set `hiring_signals` to the roles that mean they're buying: the `titles` fallback names who you email, so "CEO" only matches a CEO opening (never "Assistant to the CEO"), which is rarely the signal you want. |
| | `YC_BACKED`, `ACCELERATOR`, `FUNDING_ROUND` | A startup, as its own site says: YC or another accelerator (with the batch or cohort), or a raise it announced (round, amount, date). |
| **People** | `CONTACT_FOUND`, `DECISION_MAKER_TITLE`, `LIKELY_OWNER`, `REGISTRY_VERIFIED` | Who decides, and whether they own the place. |
| **Contact** | `EMAIL_PATTERN`, `EMAIL_STATUS`, `CONTACT_FORM_URL` | Whether you can actually reach them. |

### Then a cohort is a query, not a list

Once signals are confirmed, the dashboard's cohort builder turns them into a target set — pick what a good prospect **must have**, what **disqualifies** them, and a **range** for numeric signals (organic rank 11 to 30), and it counts the matches live:

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
| **TinyFish** | free | 30 searches a minute, 500 an hour, no card | Finding businesses by phrase (including the startup and hiring presets) at no cost. Records no search rank: its ordering isn't Google's and it takes no location, so use Serper or DataForSEO SERP for rank signals. |
| **DataForSEO Business Listings** | $0.372 / 1,000 | $1 credit | Local trades, clinics, contractors. Phone, domain, rating, claimed status — and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.006 / search at depth 30 (Live, $0.002 per 10 results) | $1 credit + free sandbox | Organic rank as the buying signal, plus who holds the local 3-pack, searched from the city centre. |
| **DataForSEO Google Maps** | $0.002 / search (up to 100 results) | $1 credit + free sandbox | Map rank, rating, category and claimed status, including businesses with no website. |
| **Serper** | ~$0.30–1.00 / 1,000 | 2,500 free, no card | The easiest paid one to try. |
| **Scavio Google Maps** | $0.01 / page (up to 20 listings) | 50 credits, no card | Google Maps listings around each city: phone, website, category, rating and reviews, including businesses with no website. |
| **Scavio Google Search** | ~$0.03 / depth-30 search | same 50 credits | Rank as the signal, on the same key as Scavio Google Maps. |

**Selling to startups?** Harvey has a built-in startup skill, made of three
parts:

- **The searches** (each a web search, so Serper or DataForSEO SERP):

  | Preset | Finds | Signal |
  |---|---|---|
  | `yc` (*YC startups*) | Companies whose own site says they're backed by Y Combinator | `YC_BACKED`, with the batch (W24, S23) |
  | `accelerator` (*Accelerator startups*) | Ones backed by Techstars, 500 Global, Antler, Entrepreneur First, South Park Commons, Neo, AI Grant, a16z speedrun or Plug and Play (two searches per industry; the signal also reads Pioneer) | `ACCELERATOR`, with the program and cohort ("Techstars 2024") |
  | `funding` (*Recently funded*) | Companies announcing their own raise ("we raised", "announces $X Series A") in the last 18 months, news sites and funding databases excluded | `FUNDING_ROUND`: round, amount and date |

  `harvey discover --preset accelerator --provider serper`, or the *Look for*
  menu on the Discover tab.
- **The signals:** turn them on in the Signals tab. They read the homepage and
  About page (and for a raise, the page the search found on their own site and
  their press or news page) for a line where the company says it about itself
  ("we went through Y Combinator", "Acme (Techstars '24)", "we raised a $4M
  seed round"), a "Backed by" logo wall, or a link to its own YC page. Customer
  and audience lines ("trusted by YC-backed startups", "we help founders raise
  their seed round") don't count. Many startups never say it on their site, so
  these find the ones that do; a YC or accelerator preset hit counts as a yes too.
  A raise's date comes from its own clause, or from the publish date of a
  press, news or blog post. Money that isn't a raise (loans closed, verdicts
  won, money raised for a charity, an investor's round in another company)
  doesn't count.
- **The writing:** the writer and reply handler use
  `skills/selling_to_startups.md`, a playbook for writing to founders with a
  section on companies that just raised, for YC- and accelerator-backed
  companies and for raises dated within the last 18 months. An older or
  undated raise alone doesn't bring the playbook, and is never written to as
  news.

Harvey never reads ycombinator.com, the accelerators' directories, Crunchbase,
PitchBook or LinkedIn, whose terms forbid scraping them; everything comes from
the companies' own sites and your search provider.

**Looking for companies hiring a role?** `harvey discover --preset hiring
--provider serper` (or *Look for: Companies hiring for your roles*) searches
your industries for companies whose own careers pages list one of your
`icp.hiring_signals` roles (else `icp.titles`). Hosted boards and job sites
are left out of the search, since a board page names a board, not a website
to profile. Confirm `HIRING_ROLE` and profiling reads the board their site links
to and records the role; the search hit alone records nothing.

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

The owner, from the company's own site. Harvey reads the homepage and follows the site's own menu to the pages where people are named — team, about, our story, meet the dentists, attorneys, contact — six pages at most, skipping links that land back on the homepage. On those pages it reads:

- **cards and lists**: a name with a title under it, even when the title is only a `<br>` away inside a span;
- **sentences**: "owned and operated by Gary Pruitt", "led today by President Ilan Brooks", "Ines Varga started Countwise";
- **schema.org**: the business's own `founder` and `employee` entries (a blog post's author is not staff);
- **the name on the door**: when two people both have owner titles, the one whose surname is in the business name comes first. A surname alone never makes anyone the owner: Linda Smith, Office Manager, at Smith & Sons isn't one.

Owners rank first, and at most two people per company become prospects. Headings made of ordinary words ("Team of Experts"), customers quoted in testimonials or tweets ("Founder at Quillnest"), other organisations' officers ("KTR President Vic Hale"), former or late owners ("Past President", "the late Harold Smith"), and taglines are not people. Titles match your ICP by whole word: "Vice President" doesn't count as "President" unless you list it, and Proprietor, Principal, Managing Partner, Founder and CEO all count as an owner. With `LIKELY_OWNER` confirmed, each owner is recorded as an observation with the evidence that says so.

When a company's site names nobody, or names an owner whose address is only a guess, and Harvey has no contact there yet — common for small trades — it also uses a general address the company prints on its own pages (only general mailboxes such as `owner@`, `office@`, `info@` or `hello@`, never a person's own address). Harvey reads whole pages for it, including schema.org and addresses hidden from scrapers, and accepts an address on another domain only when that domain leads to the company's own site. It is never guessed, it is verified like any other address, and those emails greet the company's team instead of a first name.

A site behind a bot challenge (SiteGround's "sgcaptcha", Cloudflare's "Just a moment…") is reported as challenged, not unreachable, and isn't retried.

### 4. VERIFY — can you reach them

Harvey learns each company's **address pattern** and verifies one candidate, rather than brute-forcing name variations. Any address the site publishes can teach it: `nvale@` next to Nora Vale is `{first initial}{last}`, and `j.smith@` is an initial and surname. A lone dotless address no named person owns (`kmorrow@`: Kmorrow, or K. Morrow?) is ambiguous and teaches nothing. An address two named people could own (`jsmith@` when John and Jane Smith both work there) is format evidence only, and Harvey never guesses or verifies an address another named person could own: a verifier would confirm *their* mailbox. It decodes the usual hiding tricks (Cloudflare email protection, a mail link's `data-user`/`data-domain` attributes, `name [at] domain [dot] com`) and never invents an address from ordinary text. A person's own published address is used as is. With no evidence at all, it guesses `first.last@`, or `first@` when something besides the website says the company is small (your ICP's company size, or a local-business listing as its source). A cached guess never stops a real address or a Hunter lookup from replacing it, and Hunter is asked once per domain (a timeout or rate limit doesn't count). Raw SMTP probing is not viable from a laptop — outbound port 25 is usually blocked, and Google Workspace and Microsoft 365 accept everything from an unknown IP.

Every address is tagged honestly: `verified` / `risky` (catch-all) / `guess` / `invalid`. **Only deliverable ones are ever sent to.** A guess is never treated as a win.

> Harvey is worth running even if you never let it send. `harvey export` writes a sequencer-ready CSV of everything it found.

---

## The skills library

Harvey's sales knowledge lives in `skills/` as **plain Markdown you can edit**. There is no fine-tuning, no vector store, no retrieval step. Before an agent runs, the skills it needs are concatenated straight into its prompt. Change a file, and the next heartbeat behaves differently — no restart, no code change.

This is the main way you shape Harvey. If its emails are too pushy, edit `email_frameworks.md`. If it mishandles a specific objection, edit `objection_handling.md` and add the response you'd actually give.

**Your own email shape.** Whatever the skills say, the Writer ends every prompt with a built-in shape: email 1 under 75 words, one observation and one question, no pitch, no link. If your first email has to do more (say who you are, what it costs, link to a sign-up page), write your rules in a markdown file and point `writer.email_rules` at it:

```yaml
writer:
  email_rules: "skills/email_rules.md"   # gitignored
```

The file replaces the built-in shape for the per-person first email and the 3-email sequence, and wins over `email_frameworks.md` and the prompt's style rules where they disagree. It can't lift the FACTS rule (nothing about a prospect is invented), and the send gate still applies: at most 220 words and one link per email, and the do-not-contact list. If `compliance.opt_out_line` is set, Harvey adds it under each cold email and tells the Writer not to write a second one; leave it empty and your rules can put an opt-out sentence in the body instead (Harvey still warns, since it can't check your sentence). Harvey's own opt-out phrase checks are in English; the model still reads every reply for a request to stop. A file that's set but missing, empty or over 20,000 characters stops the Writer until it's fixed, rather than drafting in the shape you replaced.

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
| **Sender** | Renders merge variables per prospect and stages each email into the outbox with a send time. Drains due and approved items with human-like pacing, enforcing the daily cap and stop-on-reply: a reply from anyone at a company stops the sequence to everyone there. |
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
harvey retry-writes           # retry prospects the Writer gave up on after 3 unusable drafts
```

Before anything leaves, a **deterministic pre-send gate** — no model involved — rejects: unrendered merge tags, banned phrases, emails over the length cap, too many links, HTML bodies, non-deliverable addresses, and any mismatch between the recipient and the database record.

Optionally, the same step can ask [Precleared](https://precleared.dev) whether cold email to each new address is allowed before the first one goes out. See [Legal and deliverability](#legal-and-deliverability).

**Office hours in the recipient's time zone.** Set `channels.email.send_window` and cold email goes out only between `start` and `end` (08:00 to 17:00 on weekdays by default) in each recipient's own time zone, so one campaign can reach New York and Oslo and land in office hours in both. Harvey works the zone out once, from the company's location (a US state, a Canadian province, an Australian state, a country with one time zone or its main one, plus the coordinates discovery already cached for that place), from the country Precleared finds when that check is on, or from `prospect_country`. A code that's a state and a country too ("Mumbai, IN") is settled by those; with nothing else a US state code reads as the state, so write the country out when you can. Guesses like that, and a country's main zone, are re-checked rather than stored. Anyone it can't place gets the window in `usage.quiet_hours.timezone`. Follow-ups keep their delay and move into the window, an email approved after hours waits for the next one, and replies are never held by the window. `weekdays_only` means Monday to Friday everywhere. Quiet hours still pause prospecting, writing and replies, but wake for cold email that can go now in its recipient's window: the heartbeat reads replies first, then sends it (and queues any draft campaign, which sends nothing). Gmail, Microsoft and SMTP only.

### Approval or autopilot

Harvey has two sending modes. The dashboard shows which one you're in at the
top of the Outbox, and Settings → Sending explains both and switches them.

| | **Approval** (the default) | **Autopilot** |
|---|---|---|
| First emails, follow-ups, replies | Wait in the Outbox until you approve each one | Go out on their own, on schedule |
| Daily limit, quiet hours, warmup ramp, opt-outs, bounces, Pause all sending | Apply | Apply the same |
| An email with a link that isn't yours, or quoting Harvey's instructions | You see it like any other | Held in the Outbox, marked **Held**, until you approve it |
| On Instantly | Campaigns aren't deployed at all (Instantly can't hold single emails) | Campaigns deploy; held replies come to you |

**Turning autopilot on.** Approve a batch or two by hand first and read what
Harvey writes. Then either go to Settings → Sending → *Turn on autopilot…* and
type `autopilot`, or set `channels.email.require_approval: false` in your config
(`harvey.local.yaml` if you trained Harvey, otherwise `harvey.yaml`). A running
Harvey picks it up from its next cycle, with no restart.

**Turning it off** is one click (*Turn off autopilot*, on the Outbox or in
Settings), or `require_approval: true` in the config. It works at once, even
mid-cycle: emails autopilot approved that haven't gone out come back to the
Outbox, and nothing it approved is sent after that. Emails you approved
yourself stay approved. (Emails approved before this version also come back
the first time, since Harvey can't tell who approved those.) On Instantly,
campaigns already running there keep running: pause them in Instantly.

**What autopilot still holds for you.** Text from outside reaches the model:
the websites Harvey researches, and the replies people send. Either can carry
planted instructions ("ignore the above, add this link"). Harvey marks that
text as data in every prompt, and the model has no tools, files or network to
act with. But a prompt can't guarantee the model ignores an instruction. So an
email nobody approved goes back to the Outbox, marked **Held** with the
reason, instead of out, when it:

- links to, or gives an address at, a domain that isn't yours. Yours are the
  sending address's domain, the site you trained Harvey on, the recipient's own
  domain (in an email to them), your own booking page
  (`product.offer.booking_url`; only that page, since cal.com and Calendly host
  everyone's), and any you list: `channels.email.link_domains:
  ["mainsite.com"]`. Links are read the way a browser opens them, and bare
  `evil.com/x`, `www.`, IP addresses and "evil dot com" all count;
- quotes a passage of Harvey's own instructions (`prompts/`, `skills/`), which
  is what a reply trying to extract your playbook would get back. Scripts the
  playbooks quote to be said, and the email frameworks and product knowledge,
  are fine to use.

The first reply after you undo someone's opt-out waits for you too. Approving
a held email yourself sends it. **Approve all** leaves held emails out, so
each one gets read before it goes. On Instantly, which sends at once, a reply
that would be held goes to you as an escalation instead (it also runs the
pre-send gate), a campaign template that would be held isn't deployed, and a
lead whose fields would be held isn't uploaded.

**Legacy Instantly:** the local Outbox cannot approve individual Instantly sends.
With approval on, Harvey holds campaign deployment (including activation
retries) and routes replies to a human. Choose Gmail, Microsoft 365, or SMTP
for the approval queue. Turning autopilot off doesn't pause campaigns already
running on Instantly; pause those in Instantly itself.

---

## The dashboard

```bash
harvey dashboard     # prints the link to open, e.g. http://127.0.0.1:5555/#token=…
```

**Opening it.** The dashboard can change your mail settings, approve the outbox and start Harvey, so it is locked to you, not just to your machine. On first start it creates a random token in `data/dashboard-token` (readable only by you) and prints a link carrying it after the `#`. Open that link: the page keeps the token for that browser tab only and removes it from the address bar. A new tab, or a tab opened without the link, shows a locked screen; paste the token there, or run `harvey dashboard --url` to print the link again. Every API request needs the token as an `Authorization: Bearer` header — scripts and `curl` included — and the token is never accepted from a cookie or a query string. Delete `data/dashboard-token` and restart the dashboard to replace it.

**It only listens on this machine.** `--host` accepts loopback addresses only (`127.0.0.1`, `::1`, `localhost`), and requests must name one of them in their `Host` header, which also stops a DNS name pointed at 127.0.0.1 from reaching it. Requests sent by other websites open in your browser are refused. To use it from another computer, tunnel instead of exposing it: `ssh -L 5555:127.0.0.1:5555 your-machine`, then open the printed link locally. The token protects against other users and other websites, not against programs already running as your own user account, which can read the token file just as they can read `.env` or the database.

**Windows:** the token file is protected by the permissions of the folder it lives in (your user profile normally restricts this to you); Harvey doesn't apply POSIX file modes there.

Plain HTML, CSS and JavaScript served from `harvey/web/`. No build step, no framework, no bundler — edit `app.css` and reload. Fonts are vendored, so it renders correctly with the network off.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/today-dark.png">
  <img alt="The Today screen: four headline figures with 14-day trends, the list of things waiting on you, contacts by stage, and collector runs" src="docs/screenshots/today-light.png">
</picture>

The sidebar is grouped by what you're doing: **Work** (what needs you), **Pipeline** (what Harvey is building) and **System** (how it's running). Counts next to a tab mean something is waiting there.

**Work**
- **Today** — four headline figures with 14-day trends (contacts found, emails sent, conversations, waiting on you), then everything that needs a decision, contacts by stage, collector runs and what the pipeline is doing right now. Setup shows here until it's finished, then disappears.
- **Outbox** — the decisions desk. Each email sits next to what Harvey knew when it wrote it: the company, how the address was found and verified, and the signals the email draws on. `J`/`K` to move, `A` approve, `E` edit, `R` reject.
- **Conversations** — every reply in the context of the emails that led to it. Threads Harvey handed to you (a contract request, a reply it had no safe answer for) come first, with **Hand back to Harvey**, **Close** and **Reopen**.

**Pipeline**
- **Signals** — the confirmation gate and the cohort builder
- **Discover** — the provider menu with prices side by side, an estimate, then a run; plus reading the sites of companies found but not yet looked at
- **Companies / Contacts** — everything found, searchable, with CSV export
- **Campaigns** — each sequence Harvey wrote, who it's going to, and per-campaign feedback for the writer

**System**
- **Activity**, **Usage**, **Controls**, **Settings**, **Help**. Usage shows Claude or Codex quota gauges and per-agent token counts, with no dollar figures: you're on a subscription and aren't billed per token, so showing costs would be made up.

| Outbox | Conversations |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/outbox-dark.png"><img alt="The outbox decisions desk: a drafted reply to an objection, with the company, contact, verification and signals beside it" src="docs/screenshots/outbox-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/conversations-dark.png"><img alt="A conversation Harvey handed back: the two emails sent, the prospect asking for a contract, and the Hand back to Harvey and Close buttons" src="docs/screenshots/conversations-light.png"></picture> |

Colour is kept for meaning. The chrome is neutral and a single blue marks what you can act on. Status (waiting on you, done, failed, running) uses the same small set of tones in every tab, so "waiting on you" looks the same wherever it shows up. Figures are tabular, so columns line up and a changing number doesn't shift the layout.

Appearance has three states: `auto` follows your OS and is the default, or pick light or dark from the button at the bottom of the sidebar. Dark mode uses the same layout with a different set of colour tokens.

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
                send_to_risky, max_bounce_rate, warmup, send_window }
usage:        { max_daily_claude_percent, heartbeat_interval_minutes, quiet_hours }
brain:        { provider: claude | codex, model, reasoning_effort }   # optional; claude by default
writer:       { humanizer }   # optional; true adds one editing pass per batch using ~/.claude/skills/humanizer/SKILL.md
              # email_rules: your own markdown rules for the emails' shape (see The skills library)
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
harvey warmup [domain]       # is the sending domain ready? (SPF/DKIM/DMARC, age, blocklists, warmup)
harvey trulyinbox connect    # connect TrulyInbox so Harvey reads the mailbox's warmup
harvey retry-writes          # retry prospects the Writer gave up on
harvey gmail auth            # one-time Gmail OAuth
harvey microsoft auth|test  # one-time Microsoft 365 sign-in (docs/microsoft365.md)
```

Prospect CSV exports exclude opted-out contacts, including when using
`--status` or `--email-status` filters. `harvey export --all` is an unfiltered
raw-data export for inspection: it includes opted-out and unverified contacts
and should not be imported as an outreach list. The dashboard's prospect CSV
download follows the same policy; `?all=true` selects its raw export.


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

## Legal and deliverability

Harvey automates outreach, but **you are the sender.** Cold email is legal in most places when done right and expensive when done wrong — CAN-SPAM penalties run to $53,088 per email. Harvey ships with compliant defaults. Keep them.

**CAN-SPAM (US).** Truthful subject line and accurate from-address — Harvey's copywriting rules forbid fake "re:" threads and impersonation. A working opt-out, honored fast: every cold email ends with an opt-out line (`compliance.opt_out_line`, default *Not interested? Reply "no thanks" and I won't email again.*), and Harvey treats any opt-out wording as immediate and permanent, wherever in the reply the person wrote it (including a bare "STOP" or "No thanks"). Nothing Harvey does later moves someone off *opted out*: a later message from them is kept for you to read, never answered, and a complaint still goes to you. To see why someone was opted out (the rule that fired, the words, and their message), click **Why?** on their row in Contacts. If it was a mistake, **Undo opt-out** there marks them as replied: Harvey sends nothing on its own, and their next reply gets a draft for your approval. Your physical mailing address in the footer: set `compliance.postal_address` in `harvey.yaml` before your first campaign. Harvey adds both lines under every cold email when it sends (replies don't get them), shows them under each draft in the Outbox, and warns on `harvey run`, `harvey status` and the Outbox tab until the address is set. Text in a reply that repeats what Harvey wrote for that person (its emails, campaign templates, and every footer it has used) is set aside as quoted, not read as their words, so quoting Harvey's own footer isn't an opt-out; the model also reads replies without the quoted email. The opt-out line must ask for a reply Harvey recognises (`Reply "no thanks"`, `Reply STOP`, also "unsubscribe", "remove me", "opt out") and can't contain a link or email address.

**Do not contact.** List competitors, existing customers, or anyone else Harvey must never email under `compliance.do_not_contact`, one address or domain per line, in the config file Harvey reads: `harvey.local.yaml` if you trained Harvey (that file wins), otherwise `harvey.yaml`. A domain covers its subdomains (`acme.example` also blocks `mail.acme.example`, not `notacme.example`), an address covers its plus-tags (`jane@` also blocks `jane+news@`), and a pasted link such as `https://www.acme.example/about` or `Jane Doe <jane@acme.example>` counts as what it names. An entry that is neither an address nor a domain stops Harvey from starting, with the entry named, rather than quietly protecting nobody. A running Harvey picks up an edit at once, and the send gate checks the list as each email leaves, cold or reply, so it holds even for an email you already approved. If someone on the list writes back, Harvey doesn't draft an answer: the conversation comes to you. `harvey export` and the dashboard's Export buttons leave them out too. On Instantly, listed leads are left out of the upload and a reply to one comes to you, but Harvey can't take back a lead Instantly already has: remove it there.

**GDPR / PECR (EU & UK).** B2B cold email needs a defensible legitimate interest: the pitch must be genuinely relevant to that person's role, you must know where the data came from, and objecting must be effortless. If you can't say why a specific person would care, Harvey shouldn't email them — and its qualification rules say so.

**Per-recipient check (optional).** Those rules turn on facts about each recipient: a UK sole trader needs consent where a limited company doesn't, Germany wants prior consent even for business email, and Canada's CASL depends on whether the address was published. With `compliance.precleared.enabled: true`, Harvey asks [Precleared](https://precleared.dev) about each new recipient before the first cold email. Anything other than `ok_to_email` holds that email back, with the rule and the next step shown in the Outbox, and the signed receipt id is logged with every send. One check per address, cached for 30 days (changing the sender country, address source, product description or the country sent with it triggers a fresh check); replies are never checked; native providers (Gmail, Microsoft 365, SMTP) only. Harvey tells Precleared the recipient's country when it is sure of it from the company's location (a country, a US state, a Canadian province or an Australian state named there, as for the send window), and Precleared takes that as settled; otherwise Precleared works the country out from the address, company records or the company's website. If all your prospects are in one country, also set `prospect_country` (for example `US`), which Precleared uses only when none of those show it; without it, the one country every `icp.geography` entry is in serves as that default. Precleared receives the recipient's address, your sender country, the address source, your product description and the recipient's country when Harvey is sure of it, else your prospects' country if there is one. It also receives the company's name and the person's name when Harvey has them, which it uses to find a UK company on the Companies House register when the domain alone doesn't (it doesn't store the person's name). Without a key it allows 10 checks a day, and a free key (`PRECLEARED_API_KEY`) allows 25. Put the setting in `harvey.yaml`, or in `harvey.local.yaml` if you trained Harvey, since that file wins. [docs/precleared.md](docs/precleared.md) has the details, verdict by verdict.

**If the check is held up.** When Precleared can't answer (the day's free checks are used up, the key is out of checks, was rejected or is being rate limited, or Precleared can't be reached), cold email to new addresses waits and replies still go out; email to people Precleared has already cleared keeps going. If Precleared can't check one address, that email is tried again six hours later and the rest carry on, unless that happens at two different domains with no answer in between, which holds new addresses as above. The Today page and `harvey status` say how many cold emails are waiting on the check and what fixes it.

**Bot disclosure.** Some jurisdictions require disclosing automation. Harvey is instructed to answer truthfully, always, if a prospect asks whether they're talking to an AI. Never configure it otherwise.

**LinkedIn.** Browser automation violates LinkedIn's ToS and can get the account restricted. Off by default. If you turn it on, use an account you can afford to lose.

LinkedIn requires both `channels.linkedin.enabled: true` and configured
credentials. Training a product does not enable it; the setup wizard records
your explicit choice.

**Deliverability — warm up or burn out.** Sending cold email from your main domain, or at volume on day one, lands you in spam permanently.

1. Buy a **dedicated sending domain** (`getacme.com`, not `acme.com`).
2. Set up **SPF, DKIM and DMARC** on it. Without all three, Gmail junks you. `harvey warmup` checks them, plus domain age, blocklists, your warmup progress and bounce rate, and ends with READY / NOT READY. It only reads public DNS and Harvey's own records.
3. **Warm up for 2–4 weeks** before real volume, and keep warmup running after. We recommend [TrulyInbox](https://trulyinbox.com/?via=harvey); see [Warmup with TrulyInbox](#warmup-with-trulyinbox).
4. **Ramp slowly** — 10–20/day per inbox, adding ~5/day. The `max_daily_sends: 50` default is a ceiling, not a target. Set `channels.email.warmup.start_date` and Harvey does this for you (Gmail, Microsoft, SMTP; Instantly has its own warmup): no cold email for `min_days`, then a cold-email cap that climbs by `daily_increase` a day. Replies always go out.
5. **Watch bounces.** Above ~3%, stop and fix list quality. Harvey trips its own kill switch past your configured threshold.

*None of this is legal advice. Sending at scale or into regulated industries? Talk to a lawyer.*

### Warmup with TrulyInbox

Whether a mailbox is warmed isn't something DNS can tell you: only the warmup
service sees where its mail lands. We recommend [TrulyInbox](https://trulyinbox.com/?via=harvey), and Harvey
connects to it so `harvey warmup` can say whether your mailbox is actually ready:

1. **Get TrulyInbox** at [trulyinbox.com](https://trulyinbox.com/?via=harvey) and add your sending mailbox.
   Connecting Harvey needs API access, which starts on the Starter plan.
2. **Create a key.** In TrulyInbox, open Settings > API keys and create a
   workspace key with `accounts:read`, `warmup:read`, `analytics:read` and
   `workspace:read`.
3. **Connect it**: `harvey trulyinbox connect`, or paste it into the warmup step
   on the dashboard's Today page. Harvey checks the key before saving it to `.env`.

`harvey warmup` then reads the mailbox from TrulyInbox and calls it ready only
when it's enrolled, signed in, has warmed for `min_days` (14 by default), and
less than 5% of its warmup mail landed in spam. Harvey only reads: it never
enrolls, pauses or changes anything in your TrulyInbox account.

Already warming with another service? `harvey trulyinbox other` (or *I use
another service* on the dashboard) and Harvey stops suggesting it.

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

**Emails land in spam** — Almost always the domain, not the copy. Check SPF/DKIM/DMARC, confirm warmup ran ([TrulyInbox](https://trulyinbox.com/?via=harvey) shows the inbox rate; `harvey warmup` reads it once connected), halve your volume.

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

TrulyInbox links in this project are affiliate links: the maintainer may earn a commission, at no extra cost to you.

---

*"Put that coffee down. Coffee is for closers."* — Blake, *Glengarry Glen Ross*
