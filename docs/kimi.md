# Thinking with Kimi

Sellbird can think with your **Kimi membership**, through the Kimi Code CLI,
instead of a Claude or ChatGPT subscription:

```yaml
brain:
  provider: kimi
  # model: ""            # optional: an alias from your Kimi config, e.g.
  #                      # "kimi-code/kimi-for-coding"; blank = its default
```

Then `kimi login` once in your own terminal — Sellbird reuses that login and
never touches your Kimi configuration, skills, plugins or session history:

- every call runs `kimi -p --output-format stream-json` under an agent file
  with `tools: []`, so the model can only answer with text — a prompt
  carrying untrusted text (prospect replies, scraped pages) gives an
  injected instruction nothing to act with;
- the call runs with `KIMI_CODE_HOME` pointed at a Sellbird-owned folder
  (`data/kimi-home/` inside your Sellbird home): your own `~/.kimi-code`
  config, global instructions, skills, plugins, MCP servers and session
  transcripts are never read or written. Only your OAuth `credentials/` is
  symlinked in, so `kimi login` stays the single sign-in, and your
  `config.toml` is *copied* (never symlinked) so model aliases keep working;
- each call runs in a fresh empty working directory, never your checkout.

Two honest differences from the Claude runtime, both properties of the Kimi
CLI rather than choices:

- the prompt travels on the command line (`-p <prompt>` has no stdin mode),
  so on a shared machine it is visible in `ps` while the call runs;
- the CLI reports no per-call token usage, so Sellbird records its own call
  counts but no token/cost rows for Kimi, and the dashboard's Usage tab has
  no live quota gauge (the membership's quota is enforced server-side).

Requirements: Kimi Code CLI with a logged-in membership (`kimi --version`
to check). If the CLI lacks `--prompt`, `--output-format` or `--agent-file`,
Sellbird pauses its brain and tells you to upgrade (`kimi upgrade`) — it
never falls back to an unisolated call.
