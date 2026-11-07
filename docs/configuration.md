# Configuration and commands

## Configuration

Two files, both plain text.

**`.env`** — credentials. Every variable documented inline with where to get it. The only required one is a mail provider; everything else is optional.

**`sellbird.yaml`** — who Sellbird is and who it sells to:

```yaml
persona:      { name, company, role, email, tone }
product:      { name, description, pricing, key_benefits, objection_responses, offer }
icp:          { industries, titles, company_size, geography, hiring_signals, geo_coordinates }
channels:
  email:      { provider: gmail | microsoft | smtp | instantly, max_daily_sends, require_approval,
                send_to_risky, min_write_score, max_bounce_rate, warmup, send_window, signature }
usage:        { max_daily_claude_percent, heartbeat_interval_minutes, quiet_hours }
brain:        { provider: claude | codex, model, reasoning_effort }   # optional; claude by default
writer:       { humanizer }   # optional; true adds one editing pass per batch using ~/.claude/skills/humanizer/SKILL.md
              # email_rules: your own markdown rules for the emails' shape (see The skills library)
```

`min_write_score` (0-100, default 0) is the lowest Scout fit score the writer
drafts for: a prospect below it keeps its deliverable address but gets no
sequence, so Claude calls and sends go to the best matches first.

`max_daily_claude_percent` caps whichever subscription `brain.provider` uses (the name predates Codex support).

If `sellbird.local.yaml` exists it wins. It's gitignored, so a fork can carry real product configuration while the tracked `sellbird.yaml` stays a template — nobody publishes their positioning and pricing by accident.

### Commands

```bash
sellbird run                   # the heartbeat loop
sellbird dashboard             # web UI at 127.0.0.1:5555 (--url: print the unlock link)
sellbird signals               # review/confirm what to prospect against
sellbird discover              # find businesses; --providers / --estimate
sellbird profile               # read discovered companies' sites (free)
sellbird train <url>           # learn a product from its website (--provider codex --model gpt-5.5)
sellbird status                # pipeline summary
sellbird usage                 # active provider's quota + per-agent tokens
sellbird outbox                # review queued email
sellbird export                # deliverable prospects → CSV
sellbird precleared-key        # free key for the cold-email check, saved to .env
sellbird companies             # company register → CSV: every company, fit, and why it wasn't contacted
sellbird sending pause|resume  # kill switch
sellbird suppressions [add|remove <value>]  # addresses and domains never emailed
sellbird warmup [domain]       # is the sending domain ready? (SPF/DKIM/DMARC, age, blocklists, warmup)
sellbird trulyinbox connect    # connect TrulyInbox so Sellbird reads the mailbox's warmup
sellbird retry-writes          # retry prospects the Writer gave up on
sellbird gmail auth            # one-time Gmail OAuth
sellbird microsoft auth|test  # one-time Microsoft 365 sign-in (docs/microsoft365.md)
```

Prospect CSV exports exclude opted-out contacts, including when using
`--status` or `--email-status` filters. `sellbird export --all` is an unfiltered
raw-data export for inspection: it includes opted-out and unverified contacts
and should not be imported as an outreach list. The dashboard's prospect CSV
download follows the same policy; `?all=true` selects its raw export.
