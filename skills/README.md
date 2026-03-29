# Skills

Skills are editable Markdown files that give Harvey foundational sales knowledge. Each agent loads the skills it needs before making decisions.

## How Skills Work

When an agent runs, it calls `brain.load_skills_for_agent("agent_name")` which concatenates the relevant skill files and injects them into the prompt. This means you can change Harvey's behavior by editing these files — no code changes needed.

## Skill → Agent Mapping

| Skill | Scout | Writer | Handler | Sender | LinkedIn |
|-------|-------|--------|---------|--------|----------|
| `prospecting_tactics.md` | x | | | | x |
| `lead_qualification.md` | x | | | | |
| `account_navigation.md` | x | | | | |
| `email_frameworks.md` | | x | | x | |
| `sales_methodology.md` | | x | x | | |
| `offer_strategy.md` | | x | x | | |
| `objection_handling.md` | | | x | | |
| `linkedin_outreach.md` | | | | | x |
| `product_knowledge.md` | x | x | x | x | x |
| `competitive_intel.md` | | x | x | | |

## Built-in Skills

- **`prospecting_tactics.md`** — Google dorking queries, company website scraping, trigger events, referral mining
- **`lead_qualification.md`** — BANT screening, ICP scoring, MEDDIC framework, disqualification criteria
- **`account_navigation.md`** — Multi-contact handling, entry point strategy, multi-threading rules
- **`email_frameworks.md`** — Cold email copywriting rules, banned AI patterns, 5 frameworks (AIDA, PAS, BAB, QVC, 3Ps)
- **`sales_methodology.md`** — ABC Loop, conversation flow, tone calibration, ethical guidelines
- **`offer_strategy.md`** — Offer ladder by engagement level, closing mechanics, timing rules
- **`objection_handling.md`** — LAARC framework, 4 objection categories with responses
- **`linkedin_outreach.md`** — Connection sequences, messaging templates, rate limits

## Auto-Generated Skills

These are created by `harvey train <url>` and are specific to your product:

- **`product_knowledge.md`** — Features, benefits, pricing, use cases, pain points, buying triggers
- **`competitive_intel.md`** — Battle cards for each competitor, differentiation angles, migration paths

## Adding or Editing Skills

1. Edit any `.md` file in this directory
2. Changes take effect on the next heartbeat cycle (no restart needed)
3. To add a new skill, create the file and add it to the `skill_map` in `harvey/brain.py`
