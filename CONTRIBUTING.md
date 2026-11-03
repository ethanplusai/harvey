# Contributing to Sellbird

For anyone changing this repository and opening pull requests, and above all
for coding agents (Claude Code, Codex, Cursor, Aider). `CLAUDE.md` is different:
it guides someone *using* Sellbird. This file is about contributing to it.

## No real contact data, anywhere it gets published

This repository is public. Sellbird's job is finding real people at real businesses,
so testing it turns up real names, addresses and phone numbers. Those stay on
your machine. A pull request once listed thirteen real people's work addresses,
with their names and employers, in its description and test fixtures, and
cleaning it up meant rewriting the branch.

**Never put any of these in a commit, test, fixture, comment, docstring, doc,
changelog entry, commit message, PR title, PR description, PR comment, review or
issue:**

- a real person's name, email address, phone number or street address;
- a real business found while testing: its name, domain, website URL or
  location, including inside HTML copied from its site;
- anything taken from `data/sellbird.db`, a user's export, an Outbox item, a reply,
  or a live test run.

**Do this instead:**

- Write fixtures by hand with fictional businesses and people. Use the reserved
  names wherever the code accepts them: `example.com`, `example.net`,
  `example.org`, and the `.example`, `.test` and `.invalid` top-level domains.
  `acme.com`-style placeholders are fine. Invented people get generic invented
  names.
- When a test needs markup that broke on a real site, rebuild the structure that
  broke (a `<br>` inside a span, a sentence shape, a schema.org block) with
  fictional content. Don't trim a copy of the real page.
- Report live checks as counts and types only: "found the decision maker on 17
  of 19 small-business sites (roofers, dentists, law firms)". Not a per-site
  table, not the addresses found, not the domains tried. Keep the detailed
  results in your local scratch directory.
- Vendors and infrastructure are fine to name, because they aren't contacts:
  Google, Cloudflare, Hunter, Instantly, DataForSEO, Greenhouse and so on.
  Companies named only for public product facts (whose job board format was
  tested) are fine too. Their staff are not.

**Before you push or open a PR**, check the diff and the description you're
about to post for addresses:

```bash
git diff origin/main | grep '^+' | grep -oE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}' | sort -u
```

Every hit should be a placeholder. If one isn't, fix it before pushing. Once
something is pushed to a public repository, deleting it later doesn't remove it:
old commits stay reachable by their hash and PR descriptions keep an edit
history.

## Test and verification email

Every test or verification email goes to an address you own, set up for that
purpose. Never to an address from a database, an export, a reply or a fixture.
Tests never send real email: use the fake providers in `tests/`.
