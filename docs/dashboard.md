# The dashboard and the data

## The dashboard

```bash
sellbird dashboard     # prints the link to open, e.g. http://127.0.0.1:5555/#token=…
```

**Opening it.** The dashboard can change your mail settings, approve the outbox and start Sellbird, so it is locked to you, not just to your machine. On first start it creates a random token in `data/dashboard-token` (readable only by you) and prints a link carrying it after the `#`. Open that link: the page keeps the token for that browser tab only and removes it from the address bar. A new tab, or a tab opened without the link, shows a locked screen; paste the token there, or run `sellbird dashboard --url` to print the link again. Every API request needs the token as an `Authorization: Bearer` header — scripts and `curl` included — and the token is never accepted from a cookie or a query string. Delete `data/dashboard-token` and restart the dashboard to replace it.

**It only listens on this machine.** `--host` accepts loopback addresses only (`127.0.0.1`, `::1`, `localhost`), and requests must name one of them in their `Host` header, which also stops a DNS name pointed at 127.0.0.1 from reaching it. Requests sent by other websites open in your browser are refused. To use it from another computer, tunnel instead of exposing it: `ssh -L 5555:127.0.0.1:5555 your-machine`, then open the printed link locally. The token protects against other users and other websites, not against programs already running as your own user account, which can read the token file just as they can read `.env` or the database.

**Windows:** the token file is protected by the permissions of the folder it lives in (your user profile normally restricts this to you); Sellbird doesn't apply POSIX file modes there.

Plain HTML, CSS and JavaScript served from `sellbird/web/`. No build step, no framework, no bundler — edit `app.css` and reload. Fonts are vendored, so it renders correctly with the network off.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="screenshots/today-dark.png">
  <img alt="The Today screen: four headline figures with 14-day trends, the list of things waiting on you, contacts by stage, and collector runs" src="screenshots/today-light.png">
</picture>

The sidebar is grouped by what you're doing: **Work** (what needs you), **Pipeline** (what Sellbird is building) and **System** (how it's running). Counts next to a tab mean something is waiting there.

**Work**
- **Today** — four headline figures with 14-day trends (contacts found, emails sent, conversations, waiting on you), then everything that needs a decision, contacts by stage, collector runs and what the pipeline is doing right now. Setup shows here until it's finished, then disappears.
- **Outbox** — the decisions desk. Each email sits next to what Sellbird knew when it wrote it: the company, how the address was found and verified, and the signals the email draws on. `J`/`K` to move, `A` approve, `E` edit, `R` reject.
- **Conversations** — every reply in the context of the emails that led to it. Threads Sellbird handed to you (a contract request, a reply it had no safe answer for) come first, with **Hand back to Sellbird**, **Close** and **Reopen**.

**Pipeline**
- **Signals** — the confirmation gate and the cohort builder
- **Discover** — the provider menu with prices side by side, an estimate, then a run; plus reading the sites of companies found but not yet looked at
- **Companies / Contacts** — everything found, searchable, with CSV export. Companies are sorted by a 0-100 fit score and show their status (emailed, draft waiting, or why not contacted); click one for its details card, or export the whole company register
- **Campaigns** — each sequence Sellbird wrote, who it's going to, and per-campaign feedback for the writer

**System**
- **Activity**, **Usage**, **Controls**, **Settings**, **Help**. Usage shows Claude or Codex quota gauges and per-agent token counts, with no dollar figures: you're on a subscription and aren't billed per token, so showing costs would be made up.

| Outbox | Conversations |
|---|---|
| <picture><source media="(prefers-color-scheme: dark)" srcset="screenshots/outbox-dark.png"><img alt="The outbox decisions desk: a drafted reply to an objection, with the company, contact, verification and signals beside it" src="screenshots/outbox-light.png"></picture> | <picture><source media="(prefers-color-scheme: dark)" srcset="screenshots/conversations-dark.png"><img alt="A conversation that needs you: the two emails Sellbird sent and the prospect's reply, marked Objection and Needs you" src="screenshots/conversations-light.png"></picture> |

Colour is kept for meaning. The chrome is neutral and a single blue marks what you can act on. Status (waiting on you, done, failed, running) uses the same small set of tones in every tab, so "waiting on you" looks the same wherever it shows up. Figures are tabular, so columns line up and a changing number doesn't shift the layout.

Appearance has three states: `auto` follows your OS and is the default, or pick light or dark from the button at the bottom of the sidebar. Dark mode uses the same layout with a different set of colour tokens.

---

## How the data is stored

SQLite at `data/sellbird.db`. One idea drives the schema:

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
