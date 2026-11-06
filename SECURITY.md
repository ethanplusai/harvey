# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately
through GitHub instead: on this repository, open **Security → Report a
vulnerability**. Only the maintainers can see the report.

Include what you found, how to reproduce it, and what an attacker could do
with it. Use fictional data in your report, as in [CONTRIBUTING.md](CONTRIBUTING.md):
never a real person's contact details or anything from a real Sellbird database.

We'll acknowledge the report within a few days, keep you updated while we work
on a fix, and credit you in the release notes unless you'd rather we didn't.

## Supported versions

Security fixes go into the latest release on `main`. Sellbird is updated with
`git pull`, so please check the problem still happens there.

## What's in scope

Sellbird runs on your own machine with your own credentials. The most
important boundaries are:

- the dashboard's sign-in token and its loopback-only listener
  ([docs/dashboard.md](docs/dashboard.md));
- the isolation of model calls, which run with no tools
  ([docs/how-it-works.md](docs/how-it-works.md));
- the Outbox holds that stop planted instructions in websites or replies from
  sending email nobody approved ([docs/approval-and-sending.md](docs/approval-and-sending.md)).

A problem that needs code already running as your own user account is out of
scope: that code can read `.env` and the database directly.

The hosted service at [sellbird.ai](https://sellbird.ai) is a separate product;
report problems with it the same way and say it's about the hosted service.
