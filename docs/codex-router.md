# Local Codex router (explicit API billing)

This optional backend is separate from `provider: codex`. It works on Windows
without weakening the subscription backend's platform or authentication checks.
It calls a local Responses-compatible router directly, not `codex exec`.
The Codex CLI and Sellbird can use the same model and router independently.

Add to your untracked `sellbird.local.yaml`:

```yaml
brain:
  provider: codex_router
  model: ar/gpt-6.1-sol
  reasoning_effort: high
  router_base_url: http://127.0.0.1:20129/v1
```

The router must already be running and must own its upstream credentials.
Sellbird never reads or copies `.codex/auth.json`, API keys, MCP settings,
plugins, hooks, or workspace instructions. Only a literal loopback IP URL with
an explicit port and `/v1` path is accepted. Redirects and environment proxies
are disabled. Requests have no tools; unexpected tool output fails closed.
There is no fallback to Claude, ChatGPT, or another model. The returned model
must match the requested model ID or its exact provider-prefix-stripped ID.
This verifies the router's reported ID, not the identity of an opaque
upstream service.

**This is not a ChatGPT subscription.** Requests may debit your AgentRouter
account. Token counts are recorded under `codex_router`; monetary cost and
account quota are unknown. A recorded zero monetary cost means unreported,
not free. Sellbird's normal daily-call cap counts router attempts, including
failed calls, but is not a monetary cap.
Use the router/account's spending limits. Timeout billing is ambiguous and
does not automatically retry. Server errors and ambiguous transport failures
also do not automatically retry, because upstream may already have billed the
request. HTTP 429 pauses further calls.

`sellbird status --check` checks router socket reachability without a paid
prompt; it does **not** verify the selected model or upstream credentials.
An explicit no-tools smoke call is needed to verify those. No discovery,
email sending, or heartbeat is required for the smoke test.
