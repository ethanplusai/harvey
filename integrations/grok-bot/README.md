# Sellbird for Grok Bot

Turn your Grok Bot into an autonomous sales agent. Grok does the thinking from your phone
or desktop; Sellbird handles the parts an agent shouldn't improvise: sending domains and
inboxes, warmup, daily limits, sending on schedule, reading replies and honoring opt-outs.
You approve every email before it goes out.

Grok Bot runs on a cloud computer, so it connects to hosted Sellbird
(`https://app.sellbird.ai/mcp`), not to a copy on your laptop.

## 1. Connect Sellbird (one message)

In Grok Bot, send:

> Add a custom MCP server called sellbird at https://app.sellbird.ai/mcp

Click **Add it**, then **Authorize**. Sign in to Sellbird and click **Allow**. That's the
whole connection: no keys to paste, and you can revoke it from Sellbird at any time.

## 2. Teach it the job (two skills)

Send:

> Install these two skills from GitHub and use them whenever I ask you to sell, find
> customers or handle replies:
> https://github.com/ethanplusai/sellbird/tree/main/integrations/grok-bot/skills/sellbird-sales-agent
> https://github.com/ethanplusai/sellbird/tree/main/integrations/grok-bot/skills/cold-email-playbook

- `sellbird-sales-agent`: how to set Sellbird up, run a sales session, and text you only
  what needs your tap.
- `cold-email-playbook`: the writing rules every email follows, so drafts sound like a
  person and stay out of spam.

## 3. Start

> Set me up on Sellbird. My website is https://example.com.

Then, once you're set up:

> Every weekday at 8:00 and 15:00, run my Sellbird session and text me only if
> something needs me.

## What it can and can't do

It can read everything in your workspace, find companies and people, write campaigns,
answer replies, record outcomes, request a domain or an inbox, and pause sending.

It can't approve an email, spend money, change DNS on a domain you brought, or resume
sending. Those are your taps: it texts you the exact link.

## Self-hosting

The open-source engine has its own MCP server for agents on your machine (Claude Code,
Codex): `sellbird mcp install`. Grok Bot can't reach your laptop, so it needs the hosted
endpoint above.
