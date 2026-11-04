# LinkedIn and public profile search

Harvey supports authenticated LinkedIn discovery and public profile discovery
through web search. Both respect `channels.linkedin.enabled`, but public search
does not require LinkedIn credentials or a browser session. A failed login does
not prevent the scout from running its remaining strategies.

## Browser configuration

```yaml
channels:
  linkedin:
    enabled: true
    user_agent: ""
    executable_path: ""
```

An empty `executable_path` selects the full Chromium executable installed by
Playwright. The `chromium` channel enables the newer headless implementation
instead of the standalone headless shell. A missing executable raises an
actionable error; there is no silent fallback to another browser.

An empty `user_agent` derives the Chrome version from `browser.version` and
uses the host platform. An explicit override must be maintained by the operator
when updating the browser. Browser selection does not guarantee that LinkedIn
will accept an automated login or remove a security challenge.

The login form accepts visible username/email fields and password fields. It
selects the exact localized sign-in button, excluding third-party sign-in
buttons, and falls back to Enter when necessary.

## Search configuration

```yaml
search:
  engine: auto
  api_key: ""
  fallback: true
  user_agent: "Harvey/0.3 (web search; https://github.com/ethanplusai/harvey)"
```

Supported engines: `auto`, `serper`, `tinyfish`, `duckduckgo`, `bing`, `google`.

- `auto` tries Serper when a key exists, then TinyFish when a key exists,
  followed by DuckDuckGo, Bing and Google.
- An explicit engine is tried first, followed by the other HTML engines when
  `fallback` is enabled. `fallback: false` uses only the selected first engine.
- `api_key` supplies the selected API engine's key; in `auto` it supplies
  Serper's key. It takes precedence over the corresponding existing
  `SERPER_API_KEY` or `TINYFISH_API_KEY`. Never commit actual keys.
- Explicitly choosing an API engine without its key reports a configuration
  error rather than pretending to have completed an empty search.

HTTP search uses HTTPX, not a browser. Its User-Agent is independent of the
LinkedIn browser configuration. Public HTML engines may return challenges,
consent pages or irrelevant results; changing a parser cannot restore access
when the response contains no search results.

Bing tracking URLs are decoded before use. DuckDuckGo supports classic HTML,
result-title links without the old wrapper, Lite tables and semantic result
cards, including `uddg` redirects. Positive `site:` operators are enforced
against host and optional path boundaries before accepting an engine's results.
Off-domain results therefore do not prevent fallback to another engine.

## Diagnostics

The stable login event is `LINKEDIN_LOGIN_BLOCKED_RECAPTCHA`. It identifies a
failed login with a detected reCAPTCHA frame, including invisible Enterprise
frames. A generic security checkpoint is not classified as reCAPTCHA. The
integration and scout can both emit the event for one attempt; monitoring
should group these lines rather than count them as independent failures.

Other login rejections use `LINKEDIN_LOGIN_FAILED reason=...`. Search has
independent events:

| Event | Meaning |
| --- | --- |
| `SCOUT_SEARCH_OK` | Accepted results |
| `SCOUT_SEARCH_BLOCKED` | Recognized anti-bot challenge |
| `SCOUT_SEARCH_ERROR` | Transport, HTTP, response-format or consent error |
| `SCOUT_SEARCH_FILTERED_OUT` | Results rejected by the requested site constraint |
| `SCOUT_SEARCH_EMPTY` | No accepted results from this engine |
| `SCOUT_SEARCH_EXHAUSTED` | All configured engines exhausted |
| `SCOUT_SEARCH_NO_API_KEY` | Only best-effort public HTML engines available |
| `SCOUT_SEARCH_CONFIG_ERROR` | Selected API engine lacks a key |

The preceding events distinguish causes of an exhausted search. This event
alone does not establish a parser bug, a login failure or a lack of demand.

## Verification

Unit tests use synthetic HTML, invented profiles and mocked browser/provider
objects. No network connection, real browser or credentials are required.

```bash
python -m pytest -q tests/test_linkedin_browser.py tests/test_linkedin_login.py \
  tests/test_scout_search_parsers.py tests/test_scout_search_config.py \
  tests/test_scout_profile_search_gate.py tests/test_scout_profile_search_flow.py
python -m pytest -q
```

Validation on Python 3.12.3 and pytest 8.4.2:

- Base `6b6a23a`: 2946 passed, 42 skipped, no failures.
- Updated implementation: 3028 passed, 42 skipped, no failures.
- 47 pre-existing warnings in each full run.
- 84 focused tests also pass with socket connections and real browser launches
  explicitly prohibited.

An optional live probe makes one query against one engine, without retries or
fallback, and reports counts only. It does not persist contacts, start the
sales engine, send messages, call an LLM or log in to LinkedIn.

```bash
python -m scripts.check_profile_search --engine duckduckgo
python -m scripts.check_profile_search --config harvey.local.yaml --engine serper
```

Without `--config`, the probe does not read local configuration or `.env`.
With `--config`, it uses existing configuration and credentials, potentially
consuming a provider query. Exit 0 means profiles were found; exit 2 means no
profiles were found. Discovery does not establish a verified email address
or independently confirm the person's current role.
