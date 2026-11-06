# How Sellbird works

## Why the subscription thing matters

Every other autonomous sales agent bills you per token. That is the whole reason they cost what they cost: an agent that thinks in a loop, all day, is an agent that burns API credits in a loop, all day. Vendors solve this by thinking less — shorter prompts, cheaper models, fewer passes.

Sellbird uses the subscription-backed CLI for the provider you choose: `claude` for Claude Code, or an isolated `codex app-server` for Codex. Both return text with model tools disabled. Sellbird handles the prospecting and mail operations itself.

Claude calls run with none of your own Claude Code setup: no `CLAUDE.md` (yours, the project's or any parent folder's), no hooks, skills, plugins, MCP servers or settings files, no tools, in an empty temporary folder, and nothing saved to your Claude transcripts. Your login (subscription or `claude setup-token`) and any admin-managed policy still apply. This needs a Claude CLI with `--safe-mode`; with an older one Sellbird pauses instead of sending prompts unisolated and says to run `claude update`. To check this offline against your installed CLI, with no credentials or network: `SELLBIRD_CLAUDE_CLI_TESTS=1 pytest tests/test_claude_cli_isolation.py`.

Your selected subscription covers model calls, subject to its usage limits. That lets Sellbird use its skills library for personalization and reply handling without a separate model API bill.

**What this costs you:**

| | |
|---|---|
| Claude Pro/Max or a ChatGPT plan with Codex access | your existing subscription |
| Finding businesses | **$0** on the default source, or ~$0.37 per 1,000 on the cheapest paid one |
| Reading their websites | **$0** — three HTTP requests each, no model call |
| Writing and sending | your subscription + a mailbox (~$7/mo Google Workspace) |
| Prospecting tools | **none.** No Apollo, no ZoomInfo, no Clearbit, no Clay. |

For comparison, the tools Sellbird replaces start at $250–500/month — and they are weakest precisely where Sellbird is strongest: small local businesses with 5–50 people, where single-provider contact coverage caps out around 30% and micro-businesses are frequently absent entirely.

**Sellbird also budgets itself.** It reads the active provider's subscription quota and throttles to leave headroom for your own Claude or Codex work. Set `max_daily_claude_percent: 80` for either provider—the setting's name predates Codex support. If quota cannot be read, Sellbird reports it as unavailable and falls back to its daily call budget.

Sellbird runs locally with your own login. Check the terms and usage limits of the provider you choose.

---

## What Sellbird actually does

Sellbird runs a heartbeat: wake up, check the budget, decide what most needs doing, do it, log it, sleep. Every 15 minutes by default.

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

## Project structure

```
sellbird/
├── main.py              # heartbeat loop
├── brain.py             # Claude/Codex provider selection + skills + usage
├── claude_runtime.py    # Isolated Claude CLI calls (safe mode, no tools, clean dir)
├── codex.py             # Isolated Codex subscription runtime
├── state.py             # SQLite schema, migrations, observations, cohorts
├── signals.py           # the 23-signal catalog Sellbird proposes
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

**Autonomous doesn't mean unsupervised.** Quiet hours, spend caps, send limits, an approval queue, a deterministic pre-send gate, and a kill switch. Sellbird asks before it spends and before it sends, until you tell it not to.

**You decide what a good prospect is.** Sellbird proposes signals; you confirm them. A prospect list you can't explain is a prospect list you shouldn't send to.

**If it can be gotten deterministically, don't use a model.** Rank position, tech detection, junk filtering, the priority decision, the pre-send gate — all plain code. Claude or Codex is reserved for the parts that need judgment. That's why this runs on a subscription at all.

**A failure is an observation, not silence.** "No team page found" is a finding. "Overpass is throttling" is a finding. Silent failures look exactly like clean results, which is how a broken run gets mistaken for an empty market.

**The database is the asset, not the agent.** The initial list is worth little. The same signals observed over quarters — who changed agencies, who started spending on ads, who let their content go stale — is worth a great deal.

**Everything is editable.** Prompts, skills, config, the dashboard. All plain text. You don't need to be a developer to change how Sellbird sells.
